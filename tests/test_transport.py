import tomllib

import pytest
from starlette.testclient import TestClient
from test_service import Backend

from microvm_mcp.adapter import manifest
from microvm_mcp.catalog import TOOLS
from microvm_mcp.server import create_app
from microvm_mcp.service import Service
from microvm_mcp.store import Store


@pytest.fixture
def api(tmp_path):
    store = Store(tmp_path / "state")
    token = store.issue("workspace")
    service = Service(store, Backend())
    with TestClient(create_app(service, ["testserver"])) as client:
        yield client, {"Authorization": f"Bearer {token}"}, store


def test_http_api_auth_and_ownership(api):
    client, headers, store = api
    assert client.post("/v1/tools/vm_list", json={}).status_code == 401
    response = client.post("/v1/tools/vm_create", headers=headers, json={"request_id": "new"})
    assert response.status_code == 200
    vm_id = response.json()["id"]
    bob = {"Authorization": f"Bearer {store.issue('bob')}"}
    assert client.post("/v1/tools/vm_status", headers=bob, json={"vm_id": vm_id}).status_code == 404
    store.revoke("workspace")
    assert client.post("/v1/tools/vm_list", headers=headers, json={}).status_code == 401


def test_http_boundary_limits(api):
    client, headers, _ = api
    assert (
        client.post(
            "/v1/tools/vm_list", headers={**headers, "Host": "evil.test"}, json={}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/tools/vm_list", headers={**headers, "Origin": "https://evil.test"}, json={}
        ).status_code
        == 403
    )
    assert (
        client.post("/v1/tools/vm_list", headers=headers, content=b"x" * 262145).status_code == 413
    )
    assert client.post("/v1/tools/vm_list", headers=headers, json=[]).status_code == 400


def test_mcp_legacy_initialize_catalog_and_call(api):
    client, headers, _ = api
    headers = {**headers, "Accept": "application/json, text/event-stream"}
    result = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
    )
    assert result.status_code == 200, result.text
    headers["MCP-Protocol-Version"] = "2025-11-25"
    result = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        },
    )
    assert len(result.json()["result"]["tools"]) == len(TOOLS), result.text
    result = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "vm_create", "arguments": {"request_id": "via-mcp"}},
        },
    )
    assert result.json()["result"]["structuredContent"]["state"] == "running", result.text


def test_generated_manifest_is_secretless_and_catalog_matches():
    data = tomllib.loads(manifest("http://127.0.0.1:8090"))
    assert len(data["tools"]) == len(TOOLS)
    for tool in TOOLS:
        generated = data["tools"][f"microvm_{tool['name']}"]
        assert generated["input_schema"] == tool["inputSchema"]
        assert generated["transport"] == "host_fetch"
        assert "workspace_id" not in generated["input_schema"]["properties"]
        assert "token" not in generated["input_schema"]["properties"]


def test_modern_mcp_catalog(api):
    client, headers, _ = api
    headers = {
        **headers,
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "tools/list",
    }
    result = client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        },
    )
    assert result.status_code == 200, result.text
    assert len(result.json()["result"]["tools"]) == len(TOOLS), result.text


def test_mcp_does_not_inherit_previous_call_authority(api):
    client, headers, store = api
    headers = {
        **headers,
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
    }

    def rpc(auth, name, args):
        return client.post(
            "/mcp",
            headers=auth,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            },
        ).json()["result"]

    vm = rpc(headers, "vm_create", {"request_id": "authority-test"})["structuredContent"]["id"]
    bob = {**headers, "Authorization": f"Bearer {store.issue('bob')}"}
    result = rpc(bob, "vm_destroy", {"vm_id": vm})
    assert result["isError"]
    assert result["structuredContent"]["error"]["code"] == "not_found"
    assert rpc(headers, "vm_status", {"vm_id": vm})["structuredContent"]["state"] == "running"


@pytest.mark.parametrize(
    "url", ["file:///tmp/x", "https://u:p@host", "https://host/x", "https://host/?q=x"]
)
def test_adapter_refuses_ambiguous_endpoints(url):
    with pytest.raises(ValueError):
        manifest(url)


@pytest.mark.parametrize("transport", ["http", "mcp"])
@pytest.mark.parametrize("encoding", [None, "base64"])
def test_output_contract_through_both_transports(api, monkeypatch, transport, encoding):
    client, headers, _ = api
    original = Backend.guest
    calls = []

    def guest(self, vm, op, args):
        if op != "job_output":
            return original(self, vm, op, args)
        calls.append(args)
        result = {"next_cursor": {"stdout": 3, "stderr": 2}, "encoding": args["encoding"]}
        if args["encoding"] == "utf-8":
            result.update(
                stdout="42\n", stderr="ø", decoding_errors={"stdout": False, "stderr": False}
            )
        else:
            result.update(stdout_base64="NDIK", stderr_base64="w7g=")
        return result

    monkeypatch.setattr(Backend, "guest", guest)
    vm = client.post("/v1/tools/vm_create", headers=headers, json={"request_id": "output"}).json()[
        "id"
    ]
    job = client.post(
        "/v1/tools/vm_exec",
        headers=headers,
        json={"vm_id": vm, "request_id": "job", "command": "unused"},
    ).json()
    args = {"vm_id": vm, **job}
    if encoding is not None:
        args["encoding"] = encoding
    if transport == "http":
        response = client.post("/v1/tools/vm_job_output", headers=headers, json=args)
        assert response.status_code == 200
        output = response.json()
    else:
        response = client.post(
            "/mcp",
            headers={
                **headers,
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-11-25",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "vm_job_output", "arguments": args},
            },
        )
        result = response.json()["result"]
        assert not result["isError"]
        output = result["structuredContent"]
    assert calls[0]["cursor"] == {"stdout": 0, "stderr": 0}
    assert calls[0]["encoding"] == (encoding or "utf-8")
    assert output["next_cursor"] == {"stdout": 3, "stderr": 2}
    if encoding is None:
        assert output["stdout"] == "42\n" and output["stderr"] == "ø"
        assert "data_base64" not in output
    else:
        assert output["stdout_base64"] == "NDIK" and output["stderr_base64"] == "w7g="


@pytest.mark.parametrize(
    "extra",
    [
        {"cursor": 0},
        {"cursor": {"stdout": -1}},
        {"cursor": {"stderr": True}},
        {"cursor": {"other": 0}},
        {"encoding": "latin-1"},
    ],
)
def test_output_schema_rejects_ambiguous_arguments(api, extra):
    client, headers, _ = api
    response = client.post(
        "/v1/tools/vm_job_output",
        headers=headers,
        json={"vm_id": "a" * 32, "job_id": "b" * 32, **extra},
    )
    assert response.status_code == 400
