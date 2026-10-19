#!/usr/bin/python3
"""Runs ONLY inside a full Linux guest. Never import as a host execution backend."""

import base64
import codecs
import ctypes
import json
import os
import select
import selectors
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

WORKSPACE = Path("/workspace")
CGROUP = Path("/sys/fs/cgroup/jobs")
OUTPUT_LIMIT = 256 * 1024
OUTPUT_CHUNK = 16 * 1024  # Per stream: at most 32 KiB of source bytes per response.
FILE_LIMIT = 128 * 1024
JOBS = {}
MANAGED_PIDS = set()
LOCK = threading.RLock()
SUSPENDED_AT = None
LIFECYCLE_PROTOCOL = 1
# Host vsock port. Not a credential. The controller replaces the placeholder bearer.
BROKER_PORT = 1023
BROKER_SOCKET = "/run/fa-broker/broker.sock"
BROKER_PLACEHOLDER = "fa-broker-placeholder"
# The listener runs as nobody: job processes (uid 1000) cannot read its route secret.
BROKER_UID = 65534
BROKER_RELAYS = 32  # Concurrent guest connections; each relay uses two threads.
BROKER_PIDS = 2 * BROKER_RELAYS + 16
HEXDIGITS = frozenset("0123456789abcdef")
CLONE_NEWNS = 0x00020000
MS_REC = 16384
MS_PRIVATE = 1 << 18


def broker_client_env():
    """Broker client shape. The capability is a placeholder, not a workspace secret.

    The process reaches that HTTP path by connecting to BROKER_SOCKET. Nothing
    here is a folder root, bucket, or cloud credential.
    """
    return {
        "FA_WORKSPACE_BACKEND": "broker",
        "FA_WORKSPACE_BROKER_URL": "http://127.0.0.1/v1/call",
        "FA_WORKSPACE_BROKER_CAPABILITY": BROKER_PLACEHOLDER,
        "FA_WORKSPACE_MAX_OBJECT_BYTES": str(64 * 1024 * 1024),
    }


def _mount_private(target, source, fstype, flags, data):
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mount.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_ulong,
        ctypes.c_char_p,
    ]
    libc.mount.restype = ctypes.c_int
    if libc.mount(source, target, fstype, flags, data) != 0:
        raise OSError(ctypes.get_errno(), f"mount failed: {target!r}")


def private_broker_mount():
    """A tmpfs visible only inside this job's mount namespace."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.unshare.argtypes = [ctypes.c_int]
    libc.unshare.restype = ctypes.c_int
    if libc.unshare(CLONE_NEWNS) != 0:
        raise OSError(ctypes.get_errno(), "unshare mount namespace failed")
    # unshare leaves mounts shared with the supervisor. Make them private before
    # creating the socket directory, or every other job would see it.
    _mount_private(b"/", None, None, MS_REC | MS_PRIVATE, None)
    os.makedirs("/run/fa-broker", mode=0o755, exist_ok=True)
    _mount_private(b"/run/fa-broker", b"tmpfs", b"tmpfs", 0, b"mode=755")


def kill_group(group):
    try:
        (Path(group) / "cgroup.kill").write_text("1")
    except FileNotFoundError:
        pass


def broker_group(job_id):
    # A sibling under CGROUP: freezing jobs freezes it, and it never keeps the job busy.
    return CGROUP / f"{job_id}-broker"


def is_hex(value, length):
    return isinstance(value, str) and len(value) == length and HEXDIGITS.issuperset(value)


def broker_preamble(job_id, route):
    """First bytes on host port 1023. The host matches the route to the job's pipe."""
    return job_id.encode() + route.encode() + b"\n"


def job_cgroup_line(group):
    return "0::/" + str(Path(group).relative_to(CGROUP.parent))


def peer_in_group(conn, group):
    """Only this job's processes may use its socket, even via /proc/<pid>/root."""
    size = struct.calcsize("3i")
    pid, _, _ = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, size))
    try:
        text = Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        return False
    return text.strip() == job_cgroup_line(group)


def relay_broker(conn, job_id, route, slots):
    """Forward one guest socket to the host after the job id and route secret."""
    vsock = None
    delay = 0.05
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            try:
                vsock = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
                vsock.connect((socket.VMADDR_CID_HOST, BROKER_PORT))
                break
            except OSError:
                if vsock is not None:
                    vsock.close()
                vsock = None
                time.sleep(delay)
                delay = min(delay * 2, 0.5)
        if vsock is None:
            return
        vsock.sendall(broker_preamble(job_id, route))

        def pump(src, dst):
            try:
                while chunk := src.recv(65536):
                    dst.sendall(chunk)
            except OSError:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

        threading.Thread(target=pump, args=(conn, vsock), daemon=True).start()
        pump(vsock, conn)
    finally:
        conn.close()
        if vsock is not None:
            vsock.close()
        slots.release()


def serve_broker(sock, job_id, route, group):
    sock.settimeout(0.5)
    events = Path(group) / "cgroup.events"
    slots = threading.BoundedSemaphore(BROKER_RELAYS)
    while True:
        try:
            if "populated 0" in events.read_text():
                break
        except FileNotFoundError:
            break
        try:
            conn, _ = sock.accept()
        except TimeoutError:
            continue
        except OSError:
            break
        if not peer_in_group(conn, group) or not slots.acquire(blocking=False):
            conn.close()
            continue
        threading.Thread(
            target=relay_broker, args=(conn, job_id, route, slots), daemon=True
        ).start()
    os._exit(0)


def run_listener(sock, job_id, route, ready_fd, status_write):
    os.close(int(ready_fd))
    devnull = os.open("/dev/null", os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    os.close(devnull)
    (broker_group(job_id) / "cgroup.procs").write_text(str(os.getpid()))
    os.setgroups([])
    os.setgid(BROKER_UID)
    os.setuid(BROKER_UID)
    os.write(status_write, b"1")
    os.close(status_write)
    serve_broker(sock, job_id, route, CGROUP / job_id)


def start_broker(job_id, route, ready_fd):
    private_broker_mount()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(BROKER_SOCKET)
    os.chown(BROKER_SOCKET, 0, 1000)
    os.chmod(BROKER_SOCKET, 0o660)
    sock.listen(16)
    # Double fork: PID 1 adopts and reaps the listener, never the customer's shell.
    # A failure here must stop the job before customer code runs.
    status_read, status_write = os.pipe()
    middle = os.fork()
    if middle == 0:
        try:
            os.close(status_read)
            if os.fork() == 0:
                run_listener(sock, job_id, route, ready_fd, status_write)
        except BaseException:
            try:
                os.write(status_write, b"0")
            except OSError:
                pass
            os._exit(1)
        os._exit(0)
    os.waitpid(middle, 0)
    os.close(status_write)
    sock.close()
    try:
        started = os.read(status_read, 1) == b"1"
    finally:
        os.close(status_read)
    if not started:
        kill_group(broker_group(job_id))
        raise OSError("broker listener failed")


def workspace_path(path):
    parts = Path(path).parts
    if Path(path).is_absolute() or ".." in parts:
        raise ValueError("path must be relative to workspace")
    return parts


def open_workspace(path, flags, mode=0o644):
    parts = workspace_path(path)
    if not parts:
        raise ValueError("file path required")
    parent = os.open(WORKSPACE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = new
        fd = os.open(parts[-1], flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode, dir_fd=parent)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError("regular files only")
        return fd
    finally:
        os.close(parent)


def populated(group):
    return "populated 1" in (group / "cgroup.events").read_text()


def execute_child(group, cwd, command, ready_fd, broker=None):
    # Enter the cgroup before dropping privileges and before customer code executes.
    (Path(group) / "cgroup.procs").write_text(str(os.getpid()))
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/home/developer",
        "LANG": "C.UTF-8",
        "USER": "developer",
    }
    if broker is not None:
        # The socket lives in this job's mount namespace. The placeholder is not a secret;
        # the route secret arrives on a pipe, never argv, and stays with the listener.
        job_id, route_fd = broker
        with os.fdopen(int(route_fd), "rb") as source:
            route = source.read(65).decode()
        if not is_hex(route, 64):
            raise ValueError("bad broker route")
        start_broker(job_id, route, ready_fd)
        env.update(broker_client_env())
    os.setgroups([])
    os.setgid(1000)
    os.setuid(1000)
    os.chdir(cwd)
    os.write(int(ready_fd), b"1")
    os.close(int(ready_fd))
    os.execve("/bin/bash", ["bash", "-lc", command], env)


def supervise(job, process, group):
    selector = selectors.DefaultSelector()
    for stream in ("stdout", "stderr"):
        pipe = getattr(process, stream)
        os.set_blocking(pipe.fileno(), False)
        selector.register(pipe, selectors.EVENT_READ, stream)
    try:
        while True:
            with LOCK:
                if SUSPENDED_AT is None and time.monotonic() >= job["deadline"]:
                    (group / "cgroup.kill").write_text("1")
                    job["timed_out"] = True
            for key, _ in selector.select(0.1):
                data = os.read(key.fd, 16384)
                if not data:
                    selector.unregister(key.fileobj)
                    with LOCK:
                        job["eof"].add(key.data)
                else:
                    with LOCK:
                        remaining = OUTPUT_LIMIT - len(job["stdout"]) - len(job["stderr"])
                        job[key.data].extend(data[:remaining])
                        job["truncated"] |= len(data) > remaining
            process.poll()  # reap the main child; descendants still count as activity
            if not populated(group) and not selector.get_map():
                break
        if job["broker_group"] is not None:
            # The listener also exits on its own once the job group empties.
            kill_group(job["broker_group"])
        with LOCK:
            job["exit_code"] = process.wait()
            job["state"] = "completed"
            MANAGED_PIDS.discard(process.pid)
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()


def summary(job):
    return {k: job[k] for k in ("state", "exit_code", "truncated", "timed_out")}


def read_output(job, args):
    """Stateless byte cursors; retain incomplete UTF-8 until its next bytes arrive."""
    encoding = args.get("encoding", "utf-8")
    if encoding not in ("utf-8", "base64"):
        raise ValueError("unsupported output encoding")
    cursor = args.get("cursor", {})
    result = {"encoding": encoding, "next_cursor": {}, "decoding_errors": {}}
    for stream in ("stdout", "stderr"):
        start = cursor.get(stream, 0)
        data = job[stream]
        if not isinstance(start, int) or isinstance(start, bool) or not 0 <= start <= len(data):
            raise ValueError("cursor beyond retained output")
        chunk = bytes(data[start : start + OUTPUT_CHUNK])
        if encoding == "base64":
            result[stream + "_base64"] = base64.b64encode(chunk).decode("ascii")
            consumed = len(chunk)
        else:
            final = start + len(chunk) == len(data) and (
                stream in job["eof"] or job["truncated"] or job["state"] == "completed"
            )
            decoder = codecs.getincrementaldecoder("utf-8")("strict")
            try:
                text = decoder.decode(chunk, final=final)
                invalid = False
            except UnicodeDecodeError:
                decoder = codecs.getincrementaldecoder("utf-8")("replace")
                text = decoder.decode(chunk, final=final)
                invalid = True
            consumed = len(chunk) - len(decoder.getstate()[0])
            result[stream] = text
            result["decoding_errors"][stream] = invalid
        result["next_cursor"][stream] = start + consumed
    if encoding == "base64":
        del result["decoding_errors"]
    return result


def prepare_suspend():
    """Freeze all customer descendants before the VMM stops or time is corrected."""
    global SUSPENDED_AT
    if SUSPENDED_AT is None:
        SUSPENDED_AT = time.monotonic()
    (CGROUP / "cgroup.freeze").write_text("1")
    deadline = time.monotonic() + 5
    while "frozen 1" not in (CGROUP / "cgroup.events").read_text().splitlines():
        if time.monotonic() >= deadline:
            # Leave timeout handling disabled and the group frozen on failure.
            raise ValueError("guest jobs did not freeze")
        time.sleep(0.01)
    return {"frozen": True}


def sync_clock(realtime_ns):
    """Host-only RPC: correct wall time before thawing jobs; preserve timeout budgets."""
    global SUSPENDED_AT
    if type(realtime_ns) is not int or not 0 <= realtime_ns < 2**63:
        raise ValueError("invalid host time")
    # CLOCK_REALTIME changes do not advance CLOCK_MONOTONIC or relative timers.
    time.clock_settime_ns(time.CLOCK_REALTIME, realtime_ns)
    if SUSPENDED_AT is not None:
        elapsed = time.monotonic() - SUSPENDED_AT
        if elapsed < 0:
            raise ValueError("guest monotonic clock moved backwards")
        # The caller holds LOCK, so timeout threads cannot race the adjustment.
        # If setting time or thawing fails, retain the suspended state.
        (CGROUP / "cgroup.freeze").write_text("0")
        for job in JOBS.values():
            if job["state"] == "running":
                job["deadline"] += elapsed
        SUSPENDED_AT = None
    return {"synchronized": True}


def handle(op, args):
    with LOCK:
        if op == "prepare_suspend":
            return prepare_suspend()
        if op == "sync_clock":
            return sync_clock(args["realtime_ns"])
        if op == "clock_status":
            return {
                "realtime_ns": time.time_ns(),
                "monotonic_ns": time.monotonic_ns(),
                "clocksource": Path(
                    "/sys/devices/system/clocksource/clocksource0/current_clocksource"
                )
                .read_text()
                .strip(),
            }
        if op == "status":
            return {
                "lifecycle_protocol": LIFECYCLE_PROTOCOL,
                "active": any(j["state"] == "running" for j in JOBS.values()),
                "jobs": {key: summary(j) for key, j in JOBS.items()},
            }
        if op == "exec":
            if SUSPENDED_AT is not None:
                raise ValueError("guest jobs are frozen")
            job_id = args["job_id"]
            if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
                raise ValueError("bad job id")
            if job_id in JOBS:
                return {"job_id": job_id}
            broker = args.get("broker_pipe") is True
            if broker and not is_hex(args.get("broker_route"), 64):
                raise ValueError("bad broker route")
            if len(JOBS) >= 256 or sum(j["state"] == "running" for j in JOBS.values()) >= 4:
                raise ValueError("job quota exceeded")
            parts = workspace_path(args.get("cwd", "."))
            cwd = WORKSPACE.joinpath(*parts).resolve(strict=True)
            if not cwd.is_relative_to(WORKSPACE):
                raise ValueError("working directory escapes workspace")
            group = CGROUP / job_id
            group.mkdir()
            (group / "pids.max").write_text("128")
            relay_group = None
            if broker:
                relay_group = broker_group(job_id)
                relay_group.mkdir()
                (relay_group / "pids.max").write_text(str(BROKER_PIDS))
            job = {
                "state": "running",
                "exit_code": None,
                "truncated": False,
                "timed_out": False,
                "stdout": bytearray(),
                "stderr": bytearray(),
                "eof": set(),
                "group": group,
                "deadline": time.monotonic() + args["timeout_seconds"],
                "broker_group": relay_group,
            }
            ready_read, ready_write = os.pipe()
            route_read = -1
            child = [
                "/usr/bin/python3",
                __file__,
                "--job-child",
            ]
            if broker:
                route_read, route_write = os.pipe()
                os.write(route_write, args["broker_route"].encode())
                os.close(route_write)
                child += ["--broker", job_id, str(route_read)]
            child += [str(group), str(cwd), args["command"], str(ready_write)]
            try:
                process = subprocess.Popen(
                    child,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                    close_fds=True,
                    pass_fds=[fd for fd in (ready_write, route_read) if fd != -1],
                )
                os.close(ready_write)
                ready_write = -1
                if not select.select([ready_read], [], [], 2)[0] or os.read(ready_read, 1) != b"1":
                    process.kill()
                    kill_group(group)
                    if relay_group is not None:
                        kill_group(relay_group)
                    process.wait()
                    process.stdout.close()
                    process.stderr.close()
                    raise ValueError("job could not enter its cgroup")
            finally:
                os.close(ready_read)
                if ready_write != -1:
                    os.close(ready_write)
                if route_read != -1:
                    os.close(route_read)
            MANAGED_PIDS.add(process.pid)
            JOBS[job_id] = job
            threading.Thread(target=supervise, args=(job, process, group), daemon=True).start()
            if broker:
                return {"job_id": job_id, "broker_pipe": True}
            return {"job_id": job_id}
        if op in ("job_status", "job_output", "job_cancel"):
            job = JOBS[args["job_id"]]
            if op == "job_cancel" and job["state"] == "running":
                (job["group"] / "cgroup.kill").write_text("1")
            result = {"job_id": args["job_id"], **summary(job)}
            if op == "job_output":
                result.update(read_output(job, args))
            return result
        if op == "read_file":
            with os.fdopen(open_workspace(args["path"], os.O_RDONLY), "rb") as src:
                data = src.read(FILE_LIMIT + 1)
            if len(data) > FILE_LIMIT:
                raise ValueError("file too large")
            return {"data_base64": base64.b64encode(data).decode()}
        if op == "write_file":
            data = base64.b64decode(args["data_base64"], validate=True)
            if len(data) > FILE_LIMIT:
                raise ValueError("file too large")
            # O_TRUNC only after fstat: opening a guest device must never have effects.
            fd = open_workspace(args["path"], os.O_WRONLY | os.O_CREAT)
            with os.fdopen(fd, "wb") as dst:
                os.ftruncate(dst.fileno(), 0)
                os.fchown(dst.fileno(), 1000, 1000)
                dst.write(data)
            return {"bytes_written": len(data)}
        raise ValueError("unknown operation")


def reap_orphans():
    # PID 1 adopts detached descendants. Do not steal Popen's main-child status.
    while True:
        with LOCK:
            for children in Path("/proc/self/task").glob("*/children"):
                try:
                    pids = children.read_text().split()
                except FileNotFoundError:
                    continue
                for value in pids:
                    pid = int(value)
                    if pid not in MANAGED_PIDS:
                        try:
                            os.waitpid(pid, os.WNOHANG)
                        except ChildProcessError:
                            pass
        time.sleep(0.1)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--job-child":
        child_args = sys.argv[2:]
        broker = None
        if child_args[:1] == ["--broker"]:
            broker = (child_args[1], child_args[2])
            child_args = child_args[3:]
        execute_child(*child_args, broker=broker)
        return
    CGROUP.mkdir(exist_ok=True)
    (CGROUP / "cgroup.subtree_control").write_text("+pids")
    threading.Thread(target=reap_orphans, daemon=True).start()
    listener = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    listener.bind((socket.VMADDR_CID_ANY, 1024))
    listener.listen(16)
    print("MICROVM_READY", flush=True)
    while True:
        # Sequential bounded RPCs. Commands themselves run asynchronously.
        connection, peer = listener.accept()
        with connection:
            if peer[0] != socket.VMADDR_CID_HOST:
                continue
            connection.settimeout(10)
            with connection.makefile("rwb") as file:
                try:
                    raw = file.readline(512 * 1024 + 1)
                    if len(raw) > 512 * 1024 or not raw.endswith(b"\n"):
                        raise ValueError("oversize request")
                    request = json.loads(raw)
                    result = handle(request["op"], request["args"])
                except Exception:
                    result = {"error": "operation failed"}
                try:
                    file.write(json.dumps(result).encode() + b"\n")
                    file.flush()
                except OSError:
                    pass


if __name__ == "__main__":
    main()
