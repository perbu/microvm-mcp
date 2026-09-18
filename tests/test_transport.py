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
