"""Exercise the public MCP transport, without starting real agent work."""

import json

import httpx
import pytest
from starlette.testclient import TestClient

from engine.apps.mcp_server.server import Settings, create_app


TOKEN = "test-secret-" * 4
SETTINGS = Settings(TOKEN, "/repos/oe", "implementation-review-rerank", "https://mini.example.ts.net")
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/json, text/event-stream",
    "Host": "mini.example.ts.net",
}


def rpc(client, method, params=None):
    return client.post("/mcp", headers=HEADERS, json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {},
    })


def test_discovery_and_immediate_creation():
    requests = []

    def upstream(request):
        requests.append(request)
        return httpx.Response(201, json={"runId": "run-123", "phase": "working"})

    with TestClient(create_app(SETTINGS, transport=httpx.MockTransport(upstream))) as client:
        initialized = rpc(client, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        })
        assert initialized.status_code == 200
        assert initialized.json()["result"]["serverInfo"]["name"] == "OpenEngine"
        tools = rpc(client, "tools/list").json()["result"]["tools"]
        assert [tool["name"] for tool in tools] == ["create_workorder"]
        assert set(tools[0]["inputSchema"]["properties"]) == {"prompt"}
        assert tools[0]["annotations"]["idempotentHint"] is False
        result = rpc(client, "tools/call", {
            "name": "create_workorder", "arguments": {"prompt": " Fix the bug "},
        }).json()["result"]
        assert not result.get("isError")
        assert result["structuredContent"] == {"run_id": "run-123"}
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == "http://127.0.0.1:8000/api/runs"
    assert "authorization" not in requests[0].headers
    assert json.loads(requests[0].content) == {
        "prompt": "Fix the bug", "repository": "/repos/oe",
        "workflowId": "implementation-review-rerank",
    }


@pytest.mark.parametrize("authorization", [None, "Bearer wrong"])
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_authentication_precedes_mcp(method, authorization):
    with TestClient(create_app(SETTINGS)) as client:
        headers = {"Authorization": authorization} if authorization else {}
        response = client.request(method, "/mcp", headers=headers)
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("prompt", ["", "   ", "x" * 100_001, 123])
def test_invalid_prompt_never_reaches_oe(prompt):
    def upstream(request):
        pytest.fail("invalid prompt reached OE")

    with TestClient(create_app(SETTINGS, transport=httpx.MockTransport(upstream))) as client:
        result = rpc(client, "tools/call", {
            "name": "create_workorder", "arguments": {"prompt": prompt},
        }).json()["result"]
        assert result["isError"]


@pytest.mark.parametrize("failure", [400, 503, "timeout", "invalid"])
def test_failures_are_tool_errors_and_not_retried(failure):
    requests = []

    def upstream(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        if failure == "invalid":
            return httpx.Response(201, json={})
        return httpx.Response(failure, text="private upstream details")

    with TestClient(create_app(SETTINGS, transport=httpx.MockTransport(upstream))) as client:
        result = rpc(client, "tools/call", {
            "name": "create_workorder", "arguments": {"prompt": "Fix the bug"},
        }).json()["result"]
        assert result["isError"]
        assert "private upstream details" not in str(result)
    assert len(requests) == 1


def test_host_and_origin_validation():
    with TestClient(create_app(SETTINGS)) as client:
        for headers in ({"Host": "evil.example"}, {"Origin": "https://evil.example"}):
            response = client.post("/mcp", headers={**HEADERS, **headers}, json={})
            assert response.status_code in (403, 421)
        assert client.get("/api/runs", headers=HEADERS).status_code == 404


@pytest.mark.parametrize("overrides", [
    {"token": ""}, {"repository": ""}, {"workflow": ""},
    {"public_url": "http://mini.example.ts.net"},
    {"public_url": "https://mini.example.ts.net/mcp"},
    {"engine_url": "https://external.example"},
])
def test_invalid_configuration_fails_closed(overrides):
    with pytest.raises(ValueError):
        Settings(**{**SETTINGS.__dict__, **overrides})
