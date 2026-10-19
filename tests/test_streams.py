import io
import os
import socket
import tempfile
import threading
import time

import anyio
import pytest
import uvicorn
from starlette.testclient import TestClient, WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect
from test_service import Backend

from microvm_mcp import server, streams
from microvm_mcp.attach import attach, stream_url
from microvm_mcp.errors import ServiceError
from microvm_mcp.server import MAX_FRAME, create_app
from microvm_mcp.service import Service
from microvm_mcp.store import Store


@pytest.fixture
def api(tmp_path):
    store = Store(tmp_path / "state")
    token = store.issue("alice", max_vms=2)
    backend = Backend()
    service = Service(store, backend, stream_ports=[[5000, 5099]], max_streams_per_vm=2)
    with TestClient(create_app(service, ["testserver"])) as client:
        headers = {"Authorization": f"Bearer {token}"}
        vm = client.post("/v1/tools/vm_create", headers=headers, json={"request_id": "c"}).json()
        yield client, headers, service, backend, vm["id"]


def path(vm, port=5000):
    return f"/v1/vms/{vm}/streams/{port}"


def drain_close(ws):
    """Read to the close frame, returning (data, close message)."""
    data = b""
    while True:
        message = ws.receive()
        if message["type"] == "websocket.close":
            return data, message
        data += message.get("bytes") or b""


def test_round_trip_and_eof(api):
    client, headers, service, backend, vm = api
    with client.websocket_connect(path(vm), headers=headers) as ws:
        # Guest bytes sent before the client speaks are delivered, not lost.
        assert ws.receive_bytes() == b"ready\n"
        ws.send_bytes(b"\x00binary\xff")
        assert ws.receive_bytes() == b"\x00binary\xff"
        assert (
            service.call(headers["Authorization"][7:], "vm_status", {"vm_id": vm})["streams"] == 1
        )
        ws.send_bytes(b"")  # EOF to the guest; the echo peer then closes.
        data, close = drain_close(ws)
    # The guest closing its end, after reading EOF, is a normal close.
    assert (data, close["code"]) == (b"", 1000)
    assert ("connect", 5000) in backend.calls
    assert service.streams.get(vm, set()) == set()


def test_guest_close_ends_stream_without_client_eof(api):
    client, headers, service, _, vm = api
    with client.websocket_connect(path(vm, 5001), headers=headers) as ws:
        data, close = drain_close(ws)
    assert (data, close["code"]) == (b"ready\n", 1000)
    assert service.streams.get(vm) == set()


@pytest.mark.parametrize(
    "mutate,status",
    [
        (lambda h, vm: ({}, path(vm)), 401),
        (lambda h, vm: ({"Authorization": "Bearer nope"}, path(vm)), 401),
        (lambda h, vm: ({**h, "Origin": "https://evil.test"}, path(vm)), 403),
        (lambda h, vm: ({**h, "Host": "evil.test"}, path(vm)), 403),
        (lambda h, vm: (h, path(vm, 1023)), 403),
        (lambda h, vm: (h, path(vm, 1024)), 403),
        (lambda h, vm: (h, path(vm, 6000)), 403),
        (lambda h, vm: (h, path("x" * 32)), 404),
        (lambda h, vm: (h, path("a" * 32)), 404),
    ],
)
def test_streams_refused_before_any_guest_connection(api, mutate, status):
    client, headers, _, backend, vm = api
    headers, url = mutate(headers, vm)
    with pytest.raises(WebSocketDenialResponse) as denial:
        with client.websocket_connect(url, headers=headers):
            pass
    assert denial.value.status_code == status
    assert not any(c[0] == "connect" for c in backend.calls)


def test_other_workspace_cannot_attach(api):
    client, _, service, backend, vm = api
    bob = {"Authorization": f"Bearer {service.store.issue('bob')}"}
    with pytest.raises(WebSocketDenialResponse) as denial:
        with client.websocket_connect(path(vm), headers=bob):
            pass
    assert denial.value.status_code == 404
    assert not any(c[0] == "connect" for c in backend.calls)


def test_missing_guest_listener_is_reported(api, monkeypatch):
    client, headers, service, _, vm = api

    def refuse(vm, port):
        raise ServiceError("port_unavailable", "No guest listener on that port", 409)

    monkeypatch.setattr(service.backend, "connect", refuse)
    with pytest.raises(WebSocketDenialResponse) as denial:
        with client.websocket_connect(path(vm), headers=headers):
            pass
    assert denial.value.status_code == 409
    assert service.streams.get(vm) == set()


def test_open_stream_blocks_idle_and_unforced_suspension(api):
    client, headers, service, backend, vm = api
    token = headers["Authorization"][7:]
    service.vms[vm]["idle_seconds"] = 1
    with client.websocket_connect(path(vm), headers=headers) as ws:
        ws.receive_bytes()
        service.vms[vm]["last_activity"] = time.time() - 10
        service.sweep()
        assert service.vms[vm]["state"] == "running"
        with pytest.raises(ServiceError, match="Open streams"):
            service.call(token, "vm_suspend", {"vm_id": vm})
        service.call(token, "vm_suspend", {"vm_id": vm, "force": True})
        _, close = drain_close(ws)
    assert (close["code"], close["reason"]) == (1001, "suspended")
    assert service.vms[vm]["state"] == "hibernated"
    # Opening a stream wakes the VM like any other workspace operation.
    with client.websocket_connect(path(vm), headers=headers) as ws:
        assert ws.receive_bytes() == b"ready\n"
    assert ("resume", "hibernated") in backend.calls


def test_destroy_closes_streams(api):
    client, headers, service, _, vm = api
    with client.websocket_connect(path(vm), headers=headers) as ws:
        ws.receive_bytes()
        service.call(headers["Authorization"][7:], "vm_destroy", {"vm_id": vm})
        _, close = drain_close(ws)
    assert (close["code"], close["reason"]) == (1001, "destroyed")


def test_stream_limits(api):
    client, headers, _, _, vm = api
    with client.websocket_connect(path(vm), headers=headers):
        with client.websocket_connect(path(vm, 5002), headers=headers):
            with pytest.raises(WebSocketDenialResponse) as denial:
                with client.websocket_connect(path(vm, 5003), headers=headers):
                    pass
    assert denial.value.status_code == 409


def test_revocation_closes_open_stream(api, monkeypatch):
    client, headers, service, _, vm = api
    monkeypatch.setattr(server, "RECHECK_SECONDS", 0.05)
    with client.websocket_connect(path(vm), headers=headers) as ws:
        ws.receive_bytes()
        service.store.revoke("alice")
        _, close = drain_close(ws)
    assert close["code"] == 1008


def test_data_after_eof_is_a_protocol_error(api):
    client, headers, service, _, vm = api
    with client.websocket_connect(path(vm), headers=headers) as ws:
        ws.receive_bytes()
        ws.send_bytes(b"")
        ws.send_bytes(b"late")
        try:
            _, close = drain_close(ws)
        except WebSocketDisconnect as exc:
            close = {"code": exc.code}
    # The echo peer may finish first (normal close); otherwise the late frame is refused.
    assert close["code"] in (1000, 1002)


@pytest.mark.parametrize(
    "ranges",
    [[[1000, 2000]], [[1023, 1023]], [[1024, 1024]], [[0, 10]], [[5, 4]], [[1, 2**32]], [[1, "2"]]],
)
def test_invalid_port_policy_fails_closed(tmp_path, ranges):
    with pytest.raises(ValueError):
        Service(Store(tmp_path / "s"), Backend(), stream_ports=ranges)


def test_rate_is_a_bounded_token_bucket(monkeypatch):
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    clock = [100.0]
    monkeypatch.setattr(streams.time, "monotonic", lambda: clock[0])
    rate = streams.Rate(1000)
    monkeypatch.setattr(streams.anyio, "sleep", sleep)

    async def scenario():
        await rate.take(1000)  # One second of burst is available immediately.
        assert slept == []
        await rate.take(500)
        assert slept == [0.5]
        clock[0] += 3600  # Long idle periods do not accumulate more than one second.
        await rate.take(2000)
        assert slept[-1] == pytest.approx(1.0)

    anyio.run(scenario)


def test_stream_url_requires_plain_origin():
    assert stream_url("https://vms.example", "a" * 32, 5000) == (
        f"wss://vms.example/v1/vms/{'a' * 32}/streams/5000"
    )
    with pytest.raises(ValueError):
        stream_url("https://u:p@vms.example", "a" * 32, 5000)


class Sink(io.RawIOBase):
    def __init__(self):
        self.data = bytearray()
        self.closed_by_eof = False

    def write(self, data):
        self.data += data
        return len(data)

    def close(self):
        self.closed_by_eof = True


def test_attach_client_through_real_uvicorn(tmp_path):
    store = Store(tmp_path / "state")
    token = store.issue("alice")
    service = Service(store, Backend())
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    host = f"127.0.0.1:{port}"
    config = uvicorn.Config(
        create_app(service, [host]), lifespan="off", ws="websockets-sansio", ws_max_size=MAX_FRAME
    )
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        while not instance.started:
            time.sleep(0.01)
        vm = service.call(token, "vm_create", {"request_id": "c"})["id"]
        payload = os.urandom(3 * 1024 * 1024)
        with tempfile.TemporaryFile() as stdin:
            stdin.write(payload)
            stdin.seek(0)
            sink = Sink()
            assert attach(f"http://{host}", token, vm, 5000, stdin, sink) == 0
        assert bytes(sink.data) == b"ready\n" + payload and sink.closed_by_eof
        assert service.streams.get(vm) == set()
        assert attach(f"http://{host}", "wrong", vm, 5000, io.BytesIO(), Sink()) == 1
    finally:
        instance.should_exit = True
        thread.join(10)
