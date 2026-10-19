"""Host-side broker pipe. The fake backend never executes guest code."""

import asyncio
import importlib.util
import json
import logging
import socket
import struct
import threading
import time
from pathlib import Path

import anyio
import pytest
from starlette.testclient import TestClient, WebSocketDenialResponse
from test_service import Backend
from test_streams import drain_close

from microvm_mcp import broker
from microvm_mcp.broker import (
    CLOSE,
    DATA,
    OPEN,
    PREAMBLE,
    BrokerPipe,
    Parser,
    ProtocolError,
    encode,
    parse_preamble,
)
from microvm_mcp.errors import ServiceError
from microvm_mcp.server import create_app
from microvm_mcp.service import Service
from microvm_mcp.store import Store

TOKEN = "a" * 43
OTHER = "b" * 43
PAYLOAD = b"broker-payload-7f3a9c"


@pytest.fixture
def api(tmp_path):
    store = Store(tmp_path / "state")
    token = store.issue("alice")
    backend = Backend()
    service = Service(store, backend, stream_ports=[[5000, 5099]])
    with TestClient(create_app(service, ["testserver"])) as client:
        headers = {"Authorization": f"Bearer {token}"}
        vm = client.post("/v1/tools/vm_create", headers=headers, json={"request_id": "c"}).json()
        yield client, headers, service, backend, vm["id"]


def guest_module():
    path = Path(__file__).parents[1] / "guest/supervisor.py"
    spec = importlib.util.spec_from_file_location("guest_broker", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def start(api, pipe_token=TOKEN, request_id="job"):
    client, headers, service, backend, vm = api
    return service.call(
        headers["Authorization"][7:],
        "vm_exec",
        {
            "vm_id": vm,
            "request_id": request_id,
            "command": "echo hi",
            "broker_pipe_token": pipe_token,
        },
    )


def broker_path(vm, job_id):
    return f"/v1/vms/{vm}/jobs/{job_id}/broker"


def route_of(backend, job_id):
    return next(a["broker_route"] for a in backend.exec_args if a["job_id"] == job_id)


def connect_guest(backend, vm, job_id, payload=b"", route=None):
    """What the job's guest listener sends to host port 1023."""
    route = route_of(backend, job_id) if route is None else route
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(3)
    sock.connect(str(backend.broker_paths[vm]))
    sock.sendall(job_id.encode() + route.encode() + b"\n" + payload)
    return sock


def refused(sock):
    """The host closed this guest connection (a reset if bytes went unread)."""
    try:
        return sock.recv(1) == b""
    except ConnectionResetError:
        return True


def receive(ws, timeout=3):
    """One websocket message, failing instead of hanging the suite."""

    async def get():
        with anyio.fail_after(timeout):
            return await ws._send_rx.receive()

    return ws.portal.call(get)


def frame(ws, timeout=3):
    message = receive(ws, timeout)
    assert message["type"] == "websocket.send", message
    return message["bytes"]


def opened(ws):
    kind, conn_id = struct.unpack(">BI", frame(ws))
    assert kind == OPEN and conn_id != 0
    return conn_id


def attach(client, headers, vm, job_id, token=TOKEN):
    return client.websocket_connect(
        broker_path(vm, job_id), headers={**headers, "X-Broker-Pipe": token}
    )


def pipe_of(service, vm, job_id):
    return service.brokers[vm][job_id]


def test_secrets_stay_out_of_receipts_status_and_logs(api, caplog):
    client, headers, service, backend, vm = api
    caplog.set_level(logging.INFO)
    job = start(api)
    assert job == {"job_id": job["job_id"], "broker_pipe": True}
    sent = backend.exec_args[-1]
    assert sent["broker_pipe"] is True
    assert sent["job_id"] == job["job_id"]
    assert "broker_pipe_token" not in sent
    # The guest gets a route secret, never the controller's token.
    route = sent["broker_route"]
    assert len(route) == 64 and TOKEN not in route
    with attach(client, headers, vm, job["job_id"]) as ws:
        guest = connect_guest(backend, vm, job["job_id"], PAYLOAD)
        conn_id = opened(ws)
        assert frame(ws) == encode(DATA, conn_id, PAYLOAD)
        guest.close()
        assert frame(ws) == encode(CLOSE, conn_id)
    status = service.call(headers["Authorization"][7:], "vm_status", {"vm_id": vm})
    for secret in (TOKEN, route):
        assert secret not in json.dumps(status)
        for path in service.store.directory.iterdir():
            if path.is_file():
                assert secret.encode() not in path.read_bytes()
        assert secret not in caplog.text
    assert PAYLOAD.decode() not in caplog.text
    assert "broker=close" in caplog.text
    # A retry may carry a different token. It returns the same job and keeps the hash.
    assert start(api, OTHER, "job") == job
    with pytest.raises(WebSocketDenialResponse) as denial:
        with attach(client, headers, vm, job["job_id"], OTHER):
            pass
    assert denial.value.status_code == 404


def test_short_token_is_rejected_before_the_guest(api):
    _, headers, service, backend, vm = api
    with pytest.raises(ServiceError, match="schema"):
        service.call(
            headers["Authorization"][7:],
            "vm_exec",
            {
                "vm_id": vm,
                "request_id": "short",
                "command": "true",
                "broker_pipe_token": "a" * 42,
            },
        )
    assert backend.exec_args == []


def test_multiplex_round_trip(api, caplog):
    client, headers, service, backend, vm = api
    caplog.set_level(logging.INFO)
    job = start(api)
    with attach(client, headers, vm, job["job_id"]) as ws:
        guest = connect_guest(backend, vm, job["job_id"], PAYLOAD)
        conn_id = opened(ws)
        assert frame(ws) == encode(DATA, conn_id, PAYLOAD)
        # A frame may be split across websocket messages.
        reply = encode(DATA, conn_id, b"pong")
        ws.send_bytes(reply[:4])
        ws.send_bytes(reply[4:])
        assert guest.recv(4) == b"pong"
        # Data queued ahead of CLOSE is delivered before the guest sees EOF.
        ws.send_bytes(encode(DATA, conn_id, b"last") + encode(CLOSE, conn_id))
        assert guest.recv(4) == b"last"
        assert guest.recv(1) == b""
        guest.close()
    assert PAYLOAD.decode() not in caplog.text
    assert TOKEN not in caplog.text
    assert pipe_of(service, vm, job["job_id"]).session is None


def test_job_id_without_its_route_secret_reaches_no_pipe(api):
    """Any guest process can dial port 1023; knowing a job id is not enough."""
    client, headers, service, backend, vm = api
    victim = start(api, request_id="victim")
    other = start(api, OTHER, "other")
    with attach(client, headers, vm, victim["job_id"]) as ws:
        for route in ("0" * 64, route_of(backend, other["job_id"])):
            forged = connect_guest(backend, vm, victim["job_id"], b"steal", route=route)
            assert refused(forged)
            forged.close()
        guest = connect_guest(backend, vm, victim["job_id"], b"mine")
        conn_id = opened(ws)
        assert frame(ws) == encode(DATA, conn_id, b"mine")
        guest.close()
    assert not pipe_of(service, vm, other["job_id"]).pending


def test_guest_bytes_queued_before_attach_are_delivered(api):
    client, headers, service, backend, vm = api
    job = start(api)
    guest = connect_guest(backend, vm, job["job_id"], b"early")
    pipe = pipe_of(service, vm, job["job_id"])
    deadline = time.monotonic() + 2
    while not pipe.pending and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pipe.pending
    with attach(client, headers, vm, job["job_id"]) as ws:
        conn_id = opened(ws)
        assert frame(ws) == encode(DATA, conn_id, b"early")
    guest.close()


def test_silent_handshake_does_not_delay_other_connections(api, monkeypatch):
    client, headers, service, backend, vm = api
    monkeypatch.setattr(broker, "HANDSHAKE_SECONDS", 30)
    job = start(api)
    silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    silent.connect(str(backend.broker_paths[vm]))
    partial = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    partial.connect(str(backend.broker_paths[vm]))
    partial.sendall(job["job_id"].encode()[:10])
    with attach(client, headers, vm, job["job_id"]) as ws:
        guest = connect_guest(backend, vm, job["job_id"], b"hi")
        conn_id = opened(ws)
        assert frame(ws, timeout=1) == encode(DATA, conn_id, b"hi")
        guest.close()
    silent.close()
    partial.close()


def test_handshake_timeout_closes_silent_connections(api, monkeypatch):
    _, _, _, backend, vm = api
    monkeypatch.setattr(broker, "HANDSHAKE_SECONDS", 0.2)
    start(api)
    silent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    silent.settimeout(3)
    silent.connect(str(backend.broker_paths[vm]))
    assert refused(silent)
    silent.close()


def test_late_frames_for_a_closed_connection_keep_the_pipe(api):
    client, headers, _, backend, vm = api
    job = start(api)
    with attach(client, headers, vm, job["job_id"]) as ws:
        first = connect_guest(backend, vm, job["job_id"])
        first_id = opened(ws)
        first.close()
        assert frame(ws) == encode(CLOSE, first_id)
        # The controller had not seen CLOSE yet when it wrote these.
        ws.send_bytes(encode(DATA, first_id, b"late") + encode(CLOSE, first_id))
        second = connect_guest(backend, vm, job["job_id"], b"still")
        second_id = opened(ws)
        assert frame(ws) == encode(DATA, second_id, b"still")
        ws.send_bytes(encode(DATA, second_id, b"ok"))
        assert second.recv(2) == b"ok"
        # An id the service never assigned is still a protocol error.
        ws.send_bytes(encode(DATA, second_id + 100, b"x"))
        _, close = drain_close(ws)
    assert close["code"] == 1002
    second.close()


def test_failed_guest_write_closes_only_that_connection(api):
    client, headers, _, backend, vm = api
    job = start(api)
    with attach(client, headers, vm, job["job_id"]) as ws:
        gone = connect_guest(backend, vm, job["job_id"])
        gone_id = opened(ws)
        live = connect_guest(backend, vm, job["job_id"])
        live_id = opened(ws)
        gone.shutdown(socket.SHUT_RD)
        gone.close()
        assert frame(ws) == encode(CLOSE, gone_id)
        ws.send_bytes(encode(DATA, live_id, b"fine"))
        assert live.recv(4) == b"fine"
        live.close()


def test_stalled_guest_loses_its_connection_not_the_pipe(api, monkeypatch):
    client, headers, _, backend, vm = api
    monkeypatch.setattr(broker, "MAX_BUFFERED", 4 * broker.MAX_PAYLOAD)
    monkeypatch.setattr(broker, "STALL_SECONDS", 0.3)
    job = start(api)
    chunk = b"x" * broker.MAX_PAYLOAD
    with attach(client, headers, vm, job["job_id"]) as ws:
        stalled = connect_guest(backend, vm, job["job_id"])
        stalled.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        stalled_id = opened(ws)
        live = connect_guest(backend, vm, job["job_id"])
        live_id = opened(ws)
        # Far more than the kernel and the per-connection buffer hold; never read.
        for _ in range(64):
            ws.send_bytes(encode(DATA, stalled_id, chunk))
        assert frame(ws, timeout=5) == encode(CLOSE, stalled_id)
        ws.send_bytes(encode(DATA, live_id, b"alive"))
        assert live.recv(5) == b"alive"
        stalled.close()
        live.close()


def test_connections_do_not_consume_threads(api):
    """The old pump held an executor thread per pipe and blocked one per send."""
    client, headers, _, backend, vm = api
    job = start(api)
    with attach(client, headers, vm, job["job_id"]) as ws:
        warm = connect_guest(backend, vm, job["job_id"])
        opened(ws)
        baseline = threading.active_count()
        guests = []
        for index in range(20):
            guests.append(connect_guest(backend, vm, job["job_id"], b"%d" % index))
            conn_id = opened(ws)
            assert frame(ws) == encode(DATA, conn_id, b"%d" % index)
        assert threading.active_count() <= baseline + 1
        for guest in [warm, *guests]:
            guest.close()


@pytest.mark.parametrize("header", [{}, {"X-Broker-Pipe": OTHER}])
def test_wrong_token_is_not_found(api, header):
    client, headers, _, _, vm = api
    job = start(api)
    with pytest.raises(WebSocketDenialResponse) as denial:
        with client.websocket_connect(
            broker_path(vm, job["job_id"]), headers={**headers, **header}
        ):
            pass
    assert denial.value.status_code == 404


def test_other_workspace_and_unknown_job_are_not_found(api):
    client, headers, service, _, vm = api
    job = start(api)
    bob = {"Authorization": f"Bearer {service.store.issue('bob')}", "X-Broker-Pipe": TOKEN}
    with pytest.raises(WebSocketDenialResponse) as denial:
        with client.websocket_connect(broker_path(vm, job["job_id"]), headers=bob):
            pass
    assert denial.value.status_code == 404
    with pytest.raises(WebSocketDenialResponse) as denial:
        with attach(client, headers, vm, "c" * 32):
            pass
    assert denial.value.status_code == 404


def test_second_attach_conflicts_until_the_first_closes(api):
    client, headers, _, _, vm = api
    job = start(api)
    with attach(client, headers, vm, job["job_id"]):
        with pytest.raises(WebSocketDenialResponse) as denial:
            with attach(client, headers, vm, job["job_id"]):
                pass
        assert denial.value.status_code == 409
    with attach(client, headers, vm, job["job_id"]):
        pass


def test_attached_pipes_per_vm_are_limited(api):
    client, headers, _, _, vm = api
    jobs = [start(api, request_id=f"job-{i}")["job_id"] for i in range(broker.MAX_ATTACHED + 1)]
    sessions = [attach(client, headers, vm, job) for job in jobs[:-1]]
    for session in sessions:
        session.__enter__()
    with pytest.raises(WebSocketDenialResponse) as denial:
        with attach(client, headers, vm, jobs[-1]):
            pass
    assert denial.value.status_code == 409
    for session in sessions:
        session.__exit__(None, None, None)


def test_stale_cleanup_leaves_a_newer_attachment(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    service = Service(Store(tmp_path / "state"), Backend())
    loop = asyncio.new_event_loop()
    try:
        pipe = BrokerPipe("v" * 32, "alice", "j" * 32, b"h", b"r" * 64)
        old = pipe.attach(loop, 0)
        pipe.detach("suspended")  # The service ends it; its endpoint has not cleaned up.
        new = pipe.attach(loop, 0)
        service.close_broker(old, "suspended")
        assert pipe.session is new
        assert new.reason is None
        service.close_broker(new, "closed")
        assert pipe.session is None
        assert caplog.text.count("broker=close") == 2
        assert "reason=suspended" in caplog.text and "reason=closed" in caplog.text
    finally:
        loop.close()


def test_revocation_closes_the_pipe(api, monkeypatch):
    client, headers, service, _, vm = api
    job = start(api)
    monkeypatch.setattr("microvm_mcp.server.RECHECK_SECONDS", 0.05)
    with attach(client, headers, vm, job["job_id"]) as ws:
        service.store.revoke("alice")
        _, close = drain_close(ws)
    assert close["code"] == 1008


def test_open_pipe_blocks_idle_and_unforced_suspension(api):
    client, headers, service, backend, vm = api
    token = headers["Authorization"][7:]
    job = start(api)
    backend.active = False
    service.vms[vm]["idle_seconds"] = 1
    with attach(client, headers, vm, job["job_id"]) as ws:
        service.vms[vm]["last_activity"] = time.time() - 10
        service.sweep()
        assert service.vms[vm]["state"] == "running"
        with pytest.raises(ServiceError, match="Open broker pipe"):
            service.call(token, "vm_suspend", {"vm_id": vm})
        service.call(token, "vm_suspend", {"vm_id": vm, "force": True})
        _, close = drain_close(ws)
    assert (close["code"], close["reason"]) == (1001, "suspended")
    assert service.vms[vm]["state"] == "hibernated"
    # Hibernation dropped the listener; resume brings it back for the same pipe.
    with attach(client, headers, vm, job["job_id"]) as ws:
        guest = connect_guest(backend, vm, job["job_id"], b"back")
        conn_id = opened(ws)
        assert frame(ws) == encode(DATA, conn_id, b"back")
        guest.close()


def test_finished_job_retires_its_pipe(api, monkeypatch):
    client, headers, service, backend, vm = api
    monkeypatch.setattr("microvm_mcp.service.BROKER_RECHECK_SECONDS", 0)
    job = start(api)
    other = start(api, OTHER, "other")
    with attach(client, headers, vm, job["job_id"]) as ws:
        backend.jobs[job["job_id"]] = {"state": "completed", "exit_code": 0}
        # The sweep notices even with idle suspension off, so the VM can idle again.
        service.vms[vm]["idle_seconds"] = 0
        service.sweep()
        _, close = drain_close(ws)
    assert (close["code"], close["reason"]) == (1001, "job finished")
    assert job["job_id"] not in service.brokers[vm]
    with pytest.raises(WebSocketDenialResponse) as denial:
        with attach(client, headers, vm, job["job_id"]):
            pass
    assert denial.value.status_code == 404
    # The listener stays while another pipe needs it, then goes.
    assert vm in service.broker_listeners
    backend.jobs[other["job_id"]] = {"state": "completed", "exit_code": 0}
    service.call(
        headers["Authorization"][7:], "vm_job_status", {"vm_id": vm, "job_id": other["job_id"]}
    )
    assert vm not in service.brokers
    assert vm not in service.broker_listeners
    assert not backend.broker_paths[vm].exists()


def test_failed_or_unsupported_exec_leaves_no_pipe(api):
    _, _, service, backend, vm = api
    backend.exec_error = ServiceError("guest_error", "Guest rejected operation", 409)
    with pytest.raises(ServiceError):
        start(api, request_id="fails")
    assert vm not in service.brokers
    backend.exec_error = None
    backend.broker_support = False
    job = start(api, request_id="old-image")
    assert job == {"job_id": job["job_id"], "broker_pipe": False}
    assert vm not in service.brokers
    assert vm not in service.broker_listeners


def test_destroy_closes_the_pipe(api):
    client, headers, service, _, vm = api
    job = start(api)
    with attach(client, headers, vm, job["job_id"]) as ws:
        service.call(headers["Authorization"][7:], "vm_destroy", {"vm_id": vm})
        _, close = drain_close(ws)
    assert (close["code"], close["reason"]) == (1001, "destroyed")
    assert vm not in service.brokers


def test_bad_frames_close_the_pipe(api):
    client, headers, _, backend, vm = api
    job = start(api)
    with attach(client, headers, vm, job["job_id"]) as ws:
        guest = connect_guest(backend, vm, job["job_id"])
        conn_id = opened(ws)
        ws.send_bytes(struct.pack(">BII", DATA, conn_id, 65537))
        _, close = drain_close(ws)
    assert close["code"] == 1002
    guest.close()


def test_parser_and_preamble():
    parser = Parser()
    assert parser.feed(encode(DATA, 7, b"abcdef")[:6]) == []
    frames = parser.feed(encode(DATA, 7, b"abcdef")[6:])
    assert frames == [(DATA, 7, b"abcdef")]
    with pytest.raises(ProtocolError):
        Parser().feed(struct.pack(">BII", DATA, 1, 65537))
    with pytest.raises(ProtocolError):
        Parser().feed(struct.pack(">BI", CLOSE, 0))
    job, route = "ab" * 16, "cd" * 32
    assert parse_preamble(f"{job}{route}\n".encode()) == (job, route.encode())
    assert parse_preamble(f"{job}\n".encode()) is None
    assert parse_preamble(f"{job}{route.upper()}\n".encode()) is None
    assert parse_preamble(f"{job}{route}x".encode()) is None


def test_guest_broker_helpers_match_the_host():
    guest = guest_module()
    env = guest.broker_client_env()
    assert env["FA_WORKSPACE_BACKEND"] == "broker"
    assert env["FA_WORKSPACE_BROKER_URL"] == "http://127.0.0.1/v1/call"
    assert env["FA_WORKSPACE_BROKER_CAPABILITY"] == "fa-broker-placeholder"
    assert guest.BROKER_SOCKET == "/run/fa-broker/broker.sock"
    assert guest.BROKER_PORT == 1023
    assert "FA_WORKSPACE_SCOPE" not in env
    assert "FA_WORKSPACE_ROOT" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    job, route = "ab" * 16, "cd" * 32
    preamble = guest.broker_preamble(job, route)
    assert len(preamble) == PREAMBLE
    assert parse_preamble(preamble) == (job, route.encode())
    assert guest.is_hex(route, 64) and not guest.is_hex(route.upper(), 64)
    assert not guest.is_hex(None, 64) and not guest.is_hex(route[:-1], 64)
    # The listener is a sibling of the job under the frozen jobs cgroup.
    assert guest.broker_group(job).parent == guest.CGROUP
    assert guest.job_cgroup_line(guest.CGROUP / job) == f"0::/jobs/{job}"
    assert guest.BROKER_UID not in (0, 1000)
