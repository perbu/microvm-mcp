#!/usr/bin/env python3
"""Enforcing real-KVM lifecycle smoke. Missing prerequisites are failures, never passes."""

import argparse
import asyncio
import base64
import fcntl
import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import uuid
from pathlib import Path

import uvicorn
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from microvm_mcp.attach import attach, stream_url
from microvm_mcp.cli import stream_settings
from microvm_mcp.errors import ServiceError
from microvm_mcp.firecracker import Firecracker
from microvm_mcp.network import names
from microvm_mcp.server import MAX_FRAME, create_app
from microvm_mcp.service import Service
from microvm_mcp.store import Store

# Guest-side test fixtures. They run only inside the microVM, started through vm_exec.
VSOCK_SERVE = b"""import socket, subprocess, sys
server = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
server.bind((socket.VMADDR_CID_ANY, int(sys.argv[1])))
server.listen()
print("listening", flush=True)
while True:
    connection, _ = server.accept()
    with connection:
        subprocess.Popen(sys.argv[2:], stdin=connection, stdout=connection)
"""
MCP_SERVER = b"""import json, os, sys
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method, params = message["method"], message.get("params", {})
    if method == "initialize":
        result = {
            "protocolVersion": params["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "guest-smoke", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": [{"name": "uname", "inputSchema": {"type": "object"}}]}
    elif method == "tools/call":
        text = os.uname().release + " " + params["arguments"]["word"]
        result = {"content": [{"type": "text", "text": text}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""


def ext4_fsid(path):
    # ext4 statfs() derives f_fsid from the superblock UUID: compare the guest's root.
    with open(path, "rb") as image:
        image.seek(1024 + 0x68)
        uuid_bytes = image.read(16)
    return int.from_bytes(uuid_bytes[:8], "little") ^ int.from_bytes(uuid_bytes[8:], "little")


class Sink(io.RawIOBase):
    def __init__(self, keep=False):
        self.digest = hashlib.sha256()
        self.size = 0
        self.eof = False
        self.data = bytearray() if keep else None

    def write(self, data):
        self.digest.update(data)
        self.size += len(data)
        if self.data is not None:
            self.data += data
        return len(data)

    def close(self):
        self.eof = True


def serve_http(service):
    """The real ASGI app and uvicorn WebSocket stack on an ephemeral loopback port."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    host = f"127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(service, [host]),
            lifespan="off",
            ws="websockets-sansio",
            ws_max_size=MAX_FRAME,
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise AssertionError("HTTP server did not start")
        time.sleep(0.01)
    return f"http://{host}", server, thread


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--network", action="store_true", help="also require public egress")
    parser.add_argument(
        "--clock-suspend-seconds",
        type=float,
        default=5,
        help="duration of each clock/timeout suspension probe (minimum 5 seconds)",
    )
    args = parser.parse_args()
    if not 5 <= args.clock_suspend_seconds <= 3600:
        parser.error("--clock-suspend-seconds must be between 5 and 3600")
    os.umask(0o077)
    config = tomllib.loads(args.config.read_text())
    store = Store(config["state_dir"])
    lock = (store.directory / "service.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    backend = Firecracker(config)
    backend.preflight()
    service = Service(store, backend, max_vms=config.get("max_vms", 16), **stream_settings(config))
    workspace = "smoke-" + uuid.uuid4().hex
    token = store.issue(workspace, max_vms=3, network=args.network)
    other = store.issue(workspace + "-other")
    created = []
    results = []

    def call(name, **kw):
        return service.call(token, name, kw)

    def check(name):
        results.append(name)
        print("PASS", name, flush=True)

    def execute_output(vm_id, command, timeout=20, encoding="utf-8"):
        job = call(
            "vm_exec",
            vm_id=vm_id,
            command=command,
            request_id=uuid.uuid4().hex,
            timeout_seconds=timeout,
        )
        deadline = time.monotonic() + timeout + 10
        while time.monotonic() < deadline:
            status = call("vm_job_status", vm_id=vm_id, **job)
            if status["state"] == "completed":
                cursor = {"stdout": 0, "stderr": 0}
                streams = {"stdout": [], "stderr": []}
                errors = {"stdout": False, "stderr": False}
                while True:
                    output = call(
                        "vm_job_output", vm_id=vm_id, cursor=cursor, encoding=encoding, **job
                    )
                    for stream in streams:
                        if encoding == "base64":
                            chunk = base64.b64decode(output[stream + "_base64"])
                        else:
                            chunk = output[stream]
                            errors[stream] |= output["decoding_errors"][stream]
                        streams[stream].append(chunk)
                    if output["next_cursor"] == cursor:
                        break
                    cursor = output["next_cursor"]
                empty = b"" if encoding == "base64" else ""
                return {
                    **status,
                    **{k: empty.join(v) for k, v in streams.items()},
                    "decoding_errors": errors,
                }
            time.sleep(0.1)
        raise AssertionError("job did not complete")

    def execute(vm_id, command, timeout=20):
        output = execute_output(vm_id, command, timeout)
        assert output["exit_code"] == 0, output
        assert not output["truncated"] and not output["timed_out"], output
        return output["stdout"]

    def check_clock(vm_id, label):
        # Bracket a direct guest sample, excluding command launch, exit polling,
        # output retrieval, and integer-second rounding from the error budget.
        before = time.time_ns()
        sample = backend.guest(service.vms[vm_id], "clock_status", {})
        after = time.time_ns()
        earliest = (sample["realtime_ns"] - after) / 1e9
        latest = (sample["realtime_ns"] - before) / 1e9
        assert -0.25 <= earliest <= latest <= 0.25, {
            "label": label,
            "guest": sample,
            "host_before_ns": before,
            "host_after_ns": after,
            "skew_seconds": [earliest, latest],
        }
        check(f"{label}: clock={sample['clocksource']} skew=[{earliest:.6f}, {latest:.6f}]s")

    def check_suspended_timeout(vm_id, mode):
        # The file gate keeps the command alive until the test explicitly releases
        # it. Suspension exceeds its entire three-second active timeout budget.
        gate = "release-" + uuid.uuid4().hex
        command = (
            'python3 -u -c \'import pathlib, time; print("ready"); '
            f'p=pathlib.Path("{gate}"); '
            'exec("while not p.exists(): time.sleep(0.01)"); print(time.time_ns())\''
        )
        job = call(
            "vm_exec", vm_id=vm_id, request_id=uuid.uuid4().hex, command=command, timeout_seconds=3
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            output = call("vm_job_output", vm_id=vm_id, **job)
            if "ready" in output["stdout"]:
                break
            time.sleep(0.01)
        else:
            raise AssertionError("clock probe did not become ready")
        call(
            "vm_suspend",
            vm_id=vm_id,
            mode="pause" if mode == "pause-to-hibernate" else mode,
            force=True,
        )
        time.sleep(args.clock_suspend_seconds)
        if mode == "pause-to-hibernate":
            call("vm_suspend", vm_id=vm_id, mode="hibernate", force=True)
        resumed_after = time.time_ns()
        call("vm_resume", vm_id=vm_id)
        check_clock(vm_id, mode)
        status = call("vm_job_status", vm_id=vm_id, **job)
        assert status["state"] == "running" and not status["timed_out"], status
        call("vm_write_file", vm_id=vm_id, path=gate, data_base64="")
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            output = call("vm_job_output", vm_id=vm_id, **job)
            if output["state"] == "completed":
                break
            time.sleep(0.01)
        else:
            raise AssertionError("clock probe did not finish")
        assert output["exit_code"] == 0 and not output["timed_out"], output
        guest_now = int(output["stdout"].splitlines()[-1])
        assert resumed_after - 250_000_000 <= guest_now <= time.time_ns() + 250_000_000, output
        check(f"{mode}: process survives suspension longer than its timeout")

    try:
        started = time.monotonic()
        vm = call(
            "vm_create", request_id=uuid.uuid4().hex, memory_mib=256, vcpus=1, idle_seconds=1
        )["id"]
        created.append(vm)
        check(f"jailed Debian boot ({time.monotonic() - started:.3f}s)")
        assert "uid=1000" in execute(vm, "id; python3 --version; node --version; gcc --version")
        check("unprivileged guest toolchains")
        source = b"""import sys
a, b = 0, 1
values = []
for _ in range(20):
    values.append(a)
    a, b = b, a + b
print(*values, sep=", ")
print("calculated 20 Fibonacci numbers", file=sys.stderr)
"""
        call(
            "vm_write_file",
            vm_id=vm,
            path="fibonacci.py",
            data_base64=base64.b64encode(source).decode(),
        )
        output = execute_output(vm, "python3 -u fibonacci.py")
        assert output["stdout"] == (
            "0, 1, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144, 233, 377, 610, 987, 1597, 2584, 4181\n"
        ), output
        assert output["stderr"] == "calculated 20 Fibonacci numbers\n", output
        assert output["exit_code"] == 0 and not any(output["decoding_errors"].values()), output
        check("Fibonacci with separate UTF-8 stdout and stderr")
        output = execute_output(
            vm, "python3 -c 'import sys; print(\"error\", file=sys.stderr); sys.exit(7)'"
        )
        assert output["stdout"] == "" and output["stderr"] == "error\n", output
        assert output["exit_code"] == 7, output
        check("stderr and nonzero exit code")
        output = execute_output(
            vm,
            'python3 -c \'import os; os.write(1, b"a" * 16383 + "😀".encode()); '
            'os.write(2, "ø".encode() * 20000)\'',
        )
        assert output["stdout"] == "a" * 16383 + "😀", output
        assert output["stderr"] == "ø" * 20000, output
        assert not any(output["decoding_errors"].values()), output
        check("UTF-8 pagination across both pipes")
        command = (
            "python3 -c 'import os; os.write(1, bytes([255, 0, 226, 130])); "
            "os.write(2, bytes([254]))'"
        )
        output = execute_output(vm, command)
        assert output["stdout"] == "�\x00�" and output["stderr"] == "�", output
        assert all(output["decoding_errors"].values()), output
        output = execute_output(vm, command, encoding="base64")
        assert output["stdout"] == b"\xff\x00\xe2\x82" and output["stderr"] == b"\xfe", output
        check("invalid UTF-8 flagged and binary output preserved on request")
        output = execute_output(
            vm, 'python3 -c \'import os; os.write(1, b"x" * 300000); os.write(2, b"y" * 300000)\''
        )
        assert output["exit_code"] == 0 and output["truncated"], output
        assert len(output["stdout"]) + len(output["stderr"]) == 256 * 1024
        check("shared output cap drains both pipes without deadlock")
        try:
            service.call(other, "vm_exec", {"vm_id": vm, "command": "id", "request_id": "deny"})
            raise AssertionError("other workspace accessed VM")
        except ServiceError as exc:
            assert exc.status == 404
        check("cross-workspace access denied")
        source = b'#include <stdio.h>\nint main(void) { puts("hello microVM"); }\n'
        call(
            "vm_write_file", vm_id=vm, path="hello.c", data_base64=base64.b64encode(source).decode()
        )
        assert "hello microVM" in execute(vm, "gcc hello.c -o hello && ./hello")
        check("write code, compile, execute")
        assert (
            base64.b64decode(call("vm_read_file", vm_id=vm, path="hello.c")["data_base64"])
            == source
        )
        execute(vm, "test $(ls /sys/class/net | wc -l) -eq 1")
        check("network off has only loopback")
        second = call("vm_create", request_id=uuid.uuid4().hex, memory_mib=256, vcpus=1)["id"]
        created.append(second)
        execute(second, "test ! -e hello.c; echo private > hello.c")
        assert (
            base64.b64decode(call("vm_read_file", vm_id=vm, path="hello.c")["data_base64"])
            == source
        )
        check("CoW disk isolation")
        call("vm_destroy", vm_id=second)
        job = call(
            "vm_exec",
            vm_id=vm,
            request_id=uuid.uuid4().hex,
            command="python3 -c 'import time; x=12345; time.sleep(3); print(x)'",
            timeout_seconds=20,
        )
        time.sleep(0.1)
        try:
            call("vm_suspend", vm_id=vm)
            raise AssertionError("busy suspension allowed")
        except ServiceError as exc:
            assert exc.code == "busy"
        call("vm_suspend", vm_id=vm, mode="pause", force=True)
        assert call("vm_status", vm_id=vm)["state"] == "paused"
        call("vm_resume", vm_id=vm)
        call("vm_suspend", vm_id=vm, mode="hibernate", force=True)
        assert vm not in backend.processes
        time.sleep(1.1)
        service = Service(
            store, backend, max_vms=config.get("max_vms", 16), **stream_settings(config)
        )
        started = time.monotonic()
        call("vm_resume", vm_id=vm)
        check(f"hibernate and restore after controller restart ({time.monotonic() - started:.3f}s)")
        for _ in range(200):
            if call("vm_job_status", vm_id=vm, **job)["state"] == "completed":
                break
            time.sleep(0.1)
        output = call("vm_job_output", vm_id=vm, **job)
        assert "12345" in output["stdout"], output
        check("process memory and output survive forced suspend")
        check_clock(vm, "guest wall clock corrected after controller restart")
        for mode in ("pause", "hibernate", "pause-to-hibernate"):
            check_suspended_timeout(vm, mode)
        output = execute_output(vm, "sleep 30", timeout=1)
        assert output["timed_out"] and output["exit_code"] != 0, output
        check("active job timeouts still enforced after repeated suspension")
        # An orphaned descendant must continue counting as activity after its shell exits.
        detached = call(
            "vm_exec",
            vm_id=vm,
            request_id=uuid.uuid4().hex,
            command="setsid sleep 2 >/dev/null 2>&1 &",
            timeout_seconds=10,
        )
        time.sleep(0.3)
        assert call("vm_job_status", vm_id=vm, **detached)["state"] == "running"
        call("vm_job_cancel", vm_id=vm, **detached)
        time.sleep(0.5)
        check("detached descendants tracked and cancelled")
        immediate = call(
            "vm_exec", vm_id=vm, request_id=uuid.uuid4().hex, command="sleep 30", timeout_seconds=40
        )
        call("vm_job_cancel", vm_id=vm, **immediate)
        time.sleep(0.3)
        assert call("vm_job_status", vm_id=vm, **immediate)["state"] == "completed"
        check("immediate cancellation after dispatch")
        time.sleep(1.2)
        service.sweep()
        assert call("vm_status", vm_id=vm)["state"] == "hibernated"
        call("vm_read_file", vm_id=vm, path="hello.c")
        assert call("vm_status", vm_id=vm)["state"] == "running"
        check("automatic hibernate and file-triggered resume")
        if args.network:
            online = call(
                "vm_create", request_id=uuid.uuid4().hex, memory_mib=256, vcpus=1, network="on"
            )["id"]
            created.append(online)
            execute(online, "curl -fsS --max-time 10 https://example.com >/dev/null")
            check("network on public TLS egress")
            execute(
                online,
                "! curl -fsS --max-time 2 http://169.254.169.254/ && "
                "! curl -fsS --max-time 2 http://10.232.0.1/",
            )
            check("metadata and private-network access denied")
            table = names(service.vms[online])[2]
            rules = json.loads(
                subprocess.check_output(["nft", "-j", "list", "table", "inet", table], text=True)
            )
            drops = [
                expr["counter"]["packets"]
                for item in rules["nftables"]
                for expr in item.get("rule", {}).get("expr", [])
                if "counter" in expr
            ]
            assert sum(drops) >= 2, "blocked traffic did not reach host denial rules"
            check("host firewall counters confirm blocked egress")
        image_ids = sorted(backend.images)
        probe = "python3 -c 'import os; print(os.statvfs(\"/\").f_fsid)'"
        fsids = {}
        for image_id in image_ids:
            image = backend.images[image_id]
            started = time.monotonic()
            vm_image = call(
                "vm_create",
                request_id=uuid.uuid4().hex,
                image=image_id,
                memory_mib=max(256, image.min_memory_mib),
                vcpus=1,
            )["id"]
            created.append(vm_image)
            boot = time.monotonic() - started
            assert call("vm_status", vm_id=vm_image)["image"] == image_id
            with (backend.directory(service.vms[vm_image]) / "kernel").open("rb") as kernel:
                assert hashlib.file_digest(kernel, "sha256").hexdigest() == image.kernel_sha256
            fsids[image_id] = ext4_fsid(image.rootfs)
            assert int(execute(vm_image, probe)) == fsids[image_id]
            call("vm_suspend", vm_id=vm_image)
            assert vm_image not in backend.processes
            call("vm_resume", vm_id=vm_image)
            assert int(execute(vm_image, probe)) == fsids[image_id]
            call("vm_destroy", vm_id=vm_image)
            check(f"image {image_id}: pinned kernel, own rootfs, hibernate/resume ({boot:.3f}s)")
        if len(image_ids) > 1:
            assert len(set(fsids.values())) == len(fsids), "configured images share a rootfs"
            restricted = store.issue(workspace + "-image", images=[image_ids[1]])
            try:
                service.call(restricted, "vm_create", {"request_id": "deny", "image": image_ids[0]})
                raise AssertionError("image restriction not enforced")
            except ServiceError as exc:
                assert exc.status == 403, exc
            check(f"token restricted to {image_ids[1]} refused for {image_ids[0]}")
        # Host-to-guest streams through the real HTTP/WebSocket stack; guest network off.
        endpoint, http_server, http_thread = serve_http(service)
        token_file = tempfile.NamedTemporaryFile("w", dir=store.directory, delete=False)
        try:
            token_file.write(token)
            token_file.close()
            for name, data in (("vsock_serve.py", VSOCK_SERVE), ("mcp_server.py", MCP_SERVER)):
                call(
                    "vm_write_file",
                    vm_id=vm,
                    path=name,
                    data_base64=base64.b64encode(data).decode(),
                )
            listeners = []
            for port, command in (
                (5000, "python3 -u mcp_server.py"),
                (5001, "sh -c 'cat > big.bin'"),
                (5002, "cat big.bin"),
                (5003, "sha256sum"),
            ):
                listener = call(
                    "vm_exec",
                    vm_id=vm,
                    request_id=uuid.uuid4().hex,
                    command=f"python3 -u vsock_serve.py {port} {command}",
                    timeout_seconds=1800,
                )
                deadline = time.monotonic() + 10
                while "listening" not in call("vm_job_output", vm_id=vm, **listener)["stdout"]:
                    assert time.monotonic() < deadline, "guest vsock listener did not start"
                    time.sleep(0.05)
                listeners.append(listener)

            async def mcp_round_trip():
                server = StdioServerParameters(
                    command=sys.executable,
                    args=[
                        "-m",
                        "microvm_mcp.cli",
                        "attach",
                        "--endpoint",
                        endpoint,
                        "--token-file",
                        token_file.name,
                        "--vm",
                        vm,
                        "--port",
                        "5000",
                    ],
                )
                async with stdio_client(server) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        result = await session.call_tool("uname", {"word": "via-vsock"})
                return [tool.name for tool in tools.tools], result.content[0].text

            listed, text = asyncio.run(mcp_round_trip())
            assert listed == ["uname"] and text.endswith(" via-vsock"), (listed, text)
            check("MCP stdio round trip through attach and a guest vsock stream")
            # Client EOF reaches the guest after all data (the upload below). Firecracker
            # has no host half-close, so a guest reply after EOF is lost; assert that
            # documented limit rather than silently depending on it.
            sink = Sink(keep=True)
            with tempfile.TemporaryFile() as stdin:
                stdin.write(b"x" * 65536)
                stdin.seek(0)
                attach(endpoint, token, vm, 5003, stdin, sink)
            assert bytes(sink.data) == b"", sink.data
            check("no half-close: guest output after client EOF is dropped, as documented")
            # Stay within four active jobs per VM: the copy check below needs an exec.
            call("vm_job_cancel", vm_id=vm, **listeners.pop())
            size = 100 * 1024**2
            digest = hashlib.sha256()
            with tempfile.TemporaryFile(dir=store.directory) as stdin:
                for _ in range(size // 1024**2):
                    chunk = os.urandom(1024**2)
                    digest.update(chunk)
                    stdin.write(chunk)
                stdin.seek(0)
                started = time.monotonic()
                assert attach(endpoint, token, vm, 5001, stdin, Sink()) == 0
                upload = time.monotonic() - started
            assert execute(vm, "sha256sum big.bin").split()[0] == digest.hexdigest()
            sink = Sink()
            started = time.monotonic()
            with open(os.devnull, "rb") as empty:
                assert attach(endpoint, token, vm, 5002, empty, sink, send_eof=False) == 0
            download = time.monotonic() - started
            assert (sink.size, sink.digest.hexdigest()) == (size, digest.hexdigest())
            check(f"100 MiB stream copy in {upload:.2f}s and out {download:.2f}s, digests match")

            def refused(credential, port):
                try:
                    with connect(
                        stream_url(endpoint, vm, port),
                        additional_headers={"Authorization": f"Bearer {credential}"},
                        open_timeout=30,
                    ):
                        pass
                except InvalidStatus as exc:
                    return exc.response.status_code
                raise AssertionError(f"stream to port {port} accepted")

            assert refused(token, 1023) == 403
            assert refused(token, 1024) == 403
            assert refused(other, 5000) == 404
            assert refused(token, 5099) == 409
            check("broker port, control port, other workspace and closed guest port refused")
            with connect(
                stream_url(endpoint, vm, 5000),
                additional_headers={"Authorization": f"Bearer {token}"},
            ) as ws:
                try:
                    call("vm_suspend", vm_id=vm, force=False)
                    raise AssertionError("suspension allowed with an open stream")
                except ServiceError as exc:
                    assert exc.code == "busy", exc
                call("vm_suspend", vm_id=vm, force=True)
                for _ in ws:
                    pass
                assert (ws.close_code, ws.close_reason) == (1001, "suspended")
            call("vm_resume", vm_id=vm)
            listed, text = asyncio.run(mcp_round_trip())
            assert text.endswith(" via-vsock"), text
            check("stream closed by hibernation; guest listener reachable after resume")
            for listener in listeners:
                call("vm_job_cancel", vm_id=vm, **listener)
        finally:
            http_server.should_exit = True
            http_thread.join(10)
            os.unlink(token_file.name)
        store.revoke(workspace)
        try:
            call("vm_list")
            raise AssertionError("revoked credential accepted")
        except ServiceError as exc:
            assert exc.status == 401
        check("revocation enforced")
        print(json.dumps({"passed": results, "backend": "real Firecracker/KVM"}, indent=2))
    except Exception:
        for vm_id, tail in backend.diagnostics.items():
            print("VMM diagnostic", vm_id, bytes(tail[-4000:]).decode(errors="replace"))
        raise
    finally:
        for vm_id in created:
            vm = service.vms[vm_id]
            backend.destroy(vm)
            vm["state"] = "destroyed"
            store.save_vm(vm)
        store.revoke(workspace)
        store.revoke(workspace + "-other")
        store.revoke(workspace + "-image")
        lock.close()


if __name__ == "__main__":
    main()
