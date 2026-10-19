"""Host-side broker pipe. The fake backend never executes guest code."""

import importlib.util
import json
import logging
import socket
import struct
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient, WebSocketDenialResponse
from test_service import Backend
from test_streams import drain_close

from microvm_mcp.broker import CLOSE, DATA, OPEN, Parser, ProtocolError, encode, read_preamble
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
    result = service.call(
        headers["Authorization"][7:],
        "vm_exec",
        {
            "vm_id": vm,
            "request_id": request_id,
            "command": "echo hi",
            "broker_pipe_token": pipe_token,
        },
    )
    return result


def broker_path(vm, job_id):
    return f"/v1/vms/{vm}/jobs/{job_id}/broker"


def connect_guest(backend, vm, job_id, payload=b""):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(2)
    sock.connect(str(backend.broker_paths[vm]))
    sock.sendall(job_id.encode() + b"\n" + payload)
    return sock


def test_token_stays_out_of_guest_receipts_status_and_logs(api, caplog):
    client, headers, service, backend, vm = api
    caplog.set_level(logging.INFO)
    with caplog.at_level(logging.INFO):
        job = start(api)
    assert job["broker_pipe"] is True
    sent = backend.exec_args[-1]
    assert sent["broker_pipe"] is True
    assert sent["job_id"] == job["job_id"]
    assert "broker_pipe_token" not in sent
    status = service.call(headers["Authorization"][7:], "vm_status", {"vm_id": vm})
    assert TOKEN not in json.dumps(status)
    for path in service.store.directory.iterdir():
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes()
    assert TOKEN not in caplog.text
    assert PAYLOAD.decode() not in caplog.text
    # A retry may carry a different token. It returns the same job and keeps the hash.
    again = start(api, OTHER, "job")
    assert again == job
    pipe_headers = {**headers, "X-Broker-Pipe": OTHER}
    with pytest.raises(WebSocketDenialResponse) as denial:
        with client.websocket_connect(broker_path(vm, job["job_id"]), headers=pipe_headers):
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
    url = broker_path(vm, job["job_id"])
    pipe_headers = {**headers, "X-Broker-Pipe": TOKEN}
    with client.websocket_connect(url, headers=pipe_headers) as ws:
        guest = connect_guest(backend, vm, job["job_id"], PAYLOAD)
        opened = ws.receive_bytes()
        data = ws.receive_bytes()
        kind, conn_id = struct.unpack(">BI", opened)
        assert (kind, opened) == (OPEN, encode(OPEN, conn_id))
        assert conn_id != 0
        assert data == encode(DATA, conn_id, PAYLOAD)
        # A frame may be split across websocket messages.
        reply = encode(DATA, conn_id, b"pong")
        ws.send_bytes(reply[:4])
        ws.send_bytes(reply[4:])
        assert guest.recv(4) == b"pong"
        ws.send_bytes(encode(CLOSE, conn_id))
        assert guest.recv(1) == b""
        guest.close()
    assert PAYLOAD.decode() not in caplog.text
    assert TOKEN not in caplog.text
    assert service.brokers[vm][job["job_id"]].attached is False


def test_guest_bytes_queued_before_attach_are_delivered(api):
    client, headers, service, backend, vm = api
    job = start(api)
    guest = connect_guest(backend, vm, job["job_id"], b"early")
    pipe = service.brokers[vm][job["job_id"]]
    deadline = time.monotonic() + 2
    while not pipe.pending and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pipe.pending
    pipe_headers = {**headers, "X-Broker-Pipe": TOKEN}
    with client.websocket_connect(broker_path(vm, job["job_id"]), headers=pipe_headers) as ws:
        opened = ws.receive_bytes()
        data = ws.receive_bytes()
        _, conn_id = struct.unpack(">BI", opened)
        assert data == encode(DATA, conn_id, b"early")
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
    missing = {**headers, "X-Broker-Pipe": TOKEN}
    with pytest.raises(WebSocketDenialResponse) as denial:
        with client.websocket_connect(broker_path(vm, "c" * 32), headers=missing):
            pass
    assert denial.value.status_code == 404


def test_second_attach_conflicts_until_the_first_closes(api):
    client, headers, _, _, vm = api
    job = start(api)
    url = broker_path(vm, job["job_id"])
    pipe_headers = {**headers, "X-Broker-Pipe": TOKEN}
    with client.websocket_connect(url, headers=pipe_headers):
        with pytest.raises(WebSocketDenialResponse) as denial:
            with client.websocket_connect(url, headers=pipe_headers):
                pass
        assert denial.value.status_code == 409
    with client.websocket_connect(url, headers=pipe_headers):
        pass


def test_revocation_closes_the_pipe(api, monkeypatch):
    client, headers, service, _, vm = api
    job = start(api)
    monkeypatch.setattr("microvm_mcp.server.RECHECK_SECONDS", 0.05)
    pipe_headers = {**headers, "X-Broker-Pipe": TOKEN}
    with client.websocket_connect(broker_path(vm, job["job_id"]), headers=pipe_headers) as ws:
        service.store.revoke("alice")
        _, close = drain_close(ws)
    assert close["code"] == 1008


def test_open_pipe_blocks_idle_and_unforced_suspension(api):
    client, headers, service, backend, vm = api
    token = headers["Authorization"][7:]
    job = start(api)
    backend.active = False
    service.vms[vm]["idle_seconds"] = 1
    pipe_headers = {**headers, "X-Broker-Pipe": TOKEN}
    with client.websocket_connect(broker_path(vm, job["job_id"]), headers=pipe_headers) as ws:
        service.vms[vm]["last_activity"] = time.time() - 10
        service.sweep()
        assert service.vms[vm]["state"] == "running"
        with pytest.raises(ServiceError, match="Open broker pipe"):
            service.call(token, "vm_suspend", {"vm_id": vm})
        service.call(token, "vm_suspend", {"vm_id": vm, "force": True})
        _, close = drain_close(ws)
    assert (close["code"], close["reason"]) == (1001, "suspended")
    assert service.vms[vm]["state"] == "hibernated"


def test_destroy_closes_the_pipe(api):
    client, headers, service, _, vm = api
    job = start(api)
    pipe_headers = {**headers, "X-Broker-Pipe": TOKEN}
    with client.websocket_connect(broker_path(vm, job["job_id"]), headers=pipe_headers) as ws:
        service.call(headers["Authorization"][7:], "vm_destroy", {"vm_id": vm})
        _, close = drain_close(ws)
    assert (close["code"], close["reason"]) == (1001, "destroyed")
    assert vm not in service.brokers


def test_bad_frames_close_the_pipe(api):
    client, headers, _, backend, vm = api
    job = start(api)
    pipe_headers = {**headers, "X-Broker-Pipe": TOKEN}
    with client.websocket_connect(broker_path(vm, job["job_id"]), headers=pipe_headers) as ws:
        guest = connect_guest(backend, vm, job["job_id"])
        opened = ws.receive_bytes()
        _, conn_id = struct.unpack(">BI", opened)
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
    left, right = socket.socketpair()
    job = "ab" * 16
    right.sendall(job.encode() + b"\nHELLO")
    assert read_preamble(left) == job
    assert left.recv(5) == b"HELLO"
    left.close()
    right.close()
    bad_left, bad_right = socket.socketpair()
    bad_right.sendall(b"nope\n")
    bad_right.close()
    assert read_preamble(bad_left) is None


def test_guest_broker_env_is_a_placeholder():
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
