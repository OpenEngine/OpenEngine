"""Exercise the public MCP transport, without starting real agent work."""

import asyncio
import json
from types import SimpleNamespace

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


@pytest.mark.parametrize("dependency", [{}, {"depends_on_run_id": None}, {"depends_on_run_id": " run-first "}])
def test_discovery_and_creation(dependency):
    requests = []

    def upstream(request):
        requests.append(request)
        phase = "scheduled" if dependency.get("depends_on_run_id") else "working"
        return httpx.Response(201, json={"runId": "run-123", "phase": phase})

    with TestClient(create_app(SETTINGS, transport=httpx.MockTransport(upstream))) as client:
        initialized = rpc(client, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        })
        assert initialized.status_code == 200
        assert initialized.json()["result"]["serverInfo"]["name"] == "OpenEngine"
        tools = rpc(client, "tools/list").json()["result"]["tools"]
        assert [tool["name"] for tool in tools] == ["create_workorder"]
        assert set(tools[0]["inputSchema"]["properties"]) == {"prompt", "depends_on_run_id"}
        assert tools[0]["inputSchema"]["required"] == ["prompt"]
        assert tools[0]["annotations"]["idempotentHint"] is False
        result = rpc(client, "tools/call", {
            "name": "create_workorder", "arguments": {"prompt": " Fix the bug ", **dependency},
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
        **({"dependsOnRunId": "run-first"} if dependency.get("depends_on_run_id") else {}),
    }


@pytest.mark.parametrize("dependency", ["", "   ", 123])
def test_invalid_dependency_never_reaches_oe(dependency):
    def upstream(request):
        pytest.fail("invalid dependency reached OE")

    with TestClient(create_app(SETTINGS, transport=httpx.MockTransport(upstream))) as client:
        result = rpc(client, "tools/call", {
            "name": "create_workorder",
            "arguments": {"prompt": "Follow up", "depends_on_run_id": dependency},
        }).json()["result"]
        assert result["isError"]


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


@pytest.fixture
def oidc():
    from dataclasses import replace
    from cryptography.hazmat.primitives.asymmetric import rsa
    import jwt
    import time

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="test", alg="RS256", use="sig")
    settings = replace(
        SETTINGS, oidc_issuer="https://issuer.example", allowed_emails=("you@example.com",),
        oidc_required_scopes=("work:create",),
    )
    requests = []
    keys = [jwk]

    def provider(request):
        requests.append(request.url.path)
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": settings.oidc_issuer, "jwks_uri": "https://issuer.example/keys",
            })
        assert request.url.path == "/keys"
        return httpx.Response(200, json={"keys": keys})

    def token(overrides=None, signing_key=None, kid="test", omit=()):
        claims = {
            "iss": settings.oidc_issuer, "sub": "user-123", "aud": settings.resource_url,
            "exp": int(time.time()) + 300, "nbf": int(time.time()) - 1,
            "email": "YOU@example.com", "email_verified": True, "scope": "work:create",
        }
        claims.update(overrides or {})
        for claim in omit:
            claims.pop(claim, None)
        return jwt.encode(claims, signing_key or key, algorithm="RS256", headers={"kid": kid})

    return settings, httpx.MockTransport(provider), token, requests, keys


def oauth_rpc(client, token):
    return client.post("/mcp", headers={**HEADERS, "Authorization": f"Bearer {token}"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
    })


@pytest.mark.parametrize("audience", [
    SETTINGS.resource_url, [SETTINGS.resource_url],
    ["https://other.example/mcp", SETTINGS.resource_url],
])
def test_oidc_acceptance_and_cache(oidc, audience):
    settings, transport, token, requests, _ = oidc
    with TestClient(create_app(settings, oidc_transport=transport)) as client:
        for _ in range(2):
            response = oauth_rpc(client, token({"aud": audience}))
            assert response.status_code == 200
            assert response.json()["result"]["tools"][0]["name"] == "create_workorder"
    assert requests == ["/.well-known/openid-configuration", "/keys"]


@pytest.mark.parametrize("claims", [
    {"email": "someone@example.com"}, {"email": None},
    {"email_verified": False}, {"email_verified": "true"},
    {"aud": "https://other.example/mcp"}, {"aud": ["https://other.example/mcp"]},
    {"aud": [SETTINGS.resource_url + "/"]},
    {"iss": "https://other.example"}, {"exp": 1}, {"exp": None},
    {"nbf": 9999999999}, {"scope": []},
])
def test_oidc_rejects_invalid_claims(oidc, claims):
    settings, transport, token, _, _ = oidc
    with TestClient(create_app(settings, oidc_transport=transport)) as client:
        assert oauth_rpc(client, token(claims)).status_code == 401


def test_oidc_bad_signature_and_unknown_key(oidc):
    from cryptography.hazmat.primitives.asymmetric import rsa
    settings, transport, token, _, _ = oidc
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with TestClient(create_app(settings, oidc_transport=transport)) as client:
        for value in (token(signing_key=wrong_key), token(kid="unknown"), "malformed", TOKEN):
            assert oauth_rpc(client, value).status_code == 401


@pytest.fixture
def oidc_clock(monkeypatch):
    from engine.apps.mcp_server import auth

    clock = [1000.0]
    monkeypatch.setattr(auth, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    return clock


def test_oidc_key_rotation(oidc, oidc_clock):
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    settings, transport, token, requests, keys = oidc
    with TestClient(create_app(settings, oidc_transport=transport)) as client:
        assert oauth_rpc(client, token()).status_code == 200
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        jwk.update(kid="rotated", alg="RS256")
        keys[:] = [jwk]
        assert oauth_rpc(client, token(signing_key=key, kid="rotated")).status_code == 401
        oidc_clock[0] += 60
        assert oauth_rpc(client, token(signing_key=key, kid="rotated")).status_code == 200
    assert requests.count("/keys") == 2


@pytest.mark.parametrize("failure_path", [None, "/.well-known/openid-configuration", "/keys"])
def test_oidc_unknown_keys_share_refresh_cooldown(oidc, oidc_clock, failure_path):
    from engine.apps.mcp_server.auth import OIDCTokenVerifier

    settings, transport, token, requests, _ = oidc
    verifier = OIDCTokenVerifier(
        settings.oidc_issuer, settings.resource_url, settings.allowed_emails, transport=transport,
    )
    attempts = []

    async def provider(request):
        attempts.append(request.url.path)
        if request.url.path == failure_path:
            return httpx.Response(503)
        return await transport.handle_async_request(request)

    async def check():
        assert await verifier.verify_token(token()) is not None
        verifier.transport = httpx.MockTransport(provider)
        for batch in range(3):
            if batch:
                oidc_clock[0] += 60
            assert all(result is None for result in await asyncio.gather(*(
                verifier.verify_token(token(kid=f"unknown-{batch}-{i}")) for i in range(20)
            )))
            assert attempts.count("/.well-known/openid-configuration") == batch
            assert attempts.count("/keys") == (0 if failure_path == "/.well-known/openid-configuration" else batch)
            assert await verifier.verify_token(token()) is not None

    asyncio.run(check())


@pytest.mark.parametrize("blocked_path", ["/.well-known/openid-configuration", "/keys"])
def test_oidc_cached_key_does_not_wait_for_refresh(oidc, oidc_clock, blocked_path):
    from engine.apps.mcp_server.auth import OIDCTokenVerifier

    settings, transport, token, _, _ = oidc

    async def check():
        started = asyncio.Event()
        release = asyncio.Event()

        async def provider(request):
            if request.url.path == blocked_path:
                started.set()
                await release.wait()
            return await transport.handle_async_request(request)

        verifier = OIDCTokenVerifier(
            settings.oidc_issuer, settings.resource_url, settings.allowed_emails, transport=transport,
        )
        assert await verifier.verify_token(token()) is not None
        verifier.transport = httpx.MockTransport(provider)
        oidc_clock[0] += 60
        refresh = asyncio.create_task(verifier.verify_token(token(kid="unknown")))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            assert await asyncio.wait_for(verifier.verify_token(token()), timeout=2) is not None
            assert not refresh.done()
        finally:
            release.set()
            assert await asyncio.wait_for(refresh, timeout=2) is None

    asyncio.run(check())


def test_oidc_public_discovery_and_challenge(oidc):
    settings, transport, token, requests, _ = oidc
    with TestClient(create_app(settings, oidc_transport=transport)) as client:
        path = "/.well-known/oauth-protected-resource/mcp"
        response = client.get(path)
        assert response.status_code == 200
        assert response.json()["resource"] == settings.resource_url
        assert response.json()["authorization_servers"] == [settings.oidc_issuer]
        assert not requests
        response = client.get("/mcp")
        assert response.status_code == 401
        challenge = response.headers["www-authenticate"]
        assert f'resource_metadata="{settings.public_url}{path}"' in challenge
        assert 'scope="work:create"' in challenge
        assert oauth_rpc(client, token({"scope": ""})).status_code == 403
        for path in ("/authorize", "/token", "/register"):
            assert client.get(path).status_code == 404


@pytest.mark.parametrize("overrides", [
    {}, {"allowed_emails": ()}, {"allowed_emails": ("",)},
    {"allowed_emails": ("   ",)}, {"oidc_issuer": ""},
    {"oidc_issuer": "http://issuer.example"}, {"oidc_audience": "https://other.example/mcp"},
    {"oidc_required_scopes": ('bad"scope',)},
])
def test_oidc_invalid_configuration(overrides):
    from dataclasses import replace
    with pytest.raises(ValueError):
        replace(SETTINGS, **{"oidc_issuer": "https://issuer.example", **overrides})


@pytest.mark.parametrize("payload", [None, [], {}, {"issuer": "https://wrong.example"}])
def test_oidc_discovery_failure_denies_access(oidc, payload):
    settings, _, token, _, _ = oidc
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    with TestClient(create_app(settings, oidc_transport=transport)) as client:
        assert oauth_rpc(client, token()).status_code == 401


def test_oidc_allowlist_log_contains_subject_only(oidc, caplog):
    settings, transport, token, _, _ = oidc
    rejected = token({"email": "someone@example.com"})
    with TestClient(create_app(settings, oidc_transport=transport)) as client:
        assert oauth_rpc(client, rejected).status_code == 401
    assert "user-123" in caplog.text
    assert "someone@example.com" not in caplog.text
    assert rejected not in caplog.text


@pytest.mark.parametrize("allowlist", [None, "", " , "])
def test_oidc_env_requires_allowlist(monkeypatch, allowlist):
    from engine.apps.mcp_server.__main__ import main

    for name, value in {
        "OE_MCP_TOKEN": TOKEN, "OE_MCP_REPOSITORY": "/repos/oe",
        "OE_MCP_WORKFLOW": "workflow", "OE_MCP_PUBLIC_URL": SETTINGS.public_url,
        "OE_MCP_OIDC_ISSUER": "https://issuer.example",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("OE_MCP_ALLOWED_EMAILS", raising=False)
    if allowlist is not None:
        monkeypatch.setenv("OE_MCP_ALLOWED_EMAILS", allowlist)
    monkeypatch.setattr("sys.argv", ["engine-mcp-server", "--env-file", "/nonexistent"])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2


@pytest.mark.parametrize("value", [None, False, "true", 1, "missing"])
def test_oidc_requires_verified_email_with_clear_log(oidc, caplog, value):
    settings, transport, token, _, _ = oidc
    rejected = token({"email_verified": value}, omit=("email_verified",) if value == "missing" else ())
    with TestClient(create_app(settings, oidc_transport=transport)) as client:
        assert oauth_rpc(client, rejected).status_code == 401
    assert "email_verified must be present and boolean true" in caplog.text
    assert "configure the identity provider" in caplog.text
    assert "user-123" in caplog.text
    assert "YOU@example.com" not in caplog.text
    assert rejected not in caplog.text


@pytest.mark.parametrize("field,value,name", [
    ("oidc_audience", SETTINGS.resource_url, "OE_MCP_OIDC_AUDIENCE"),
    ("oidc_audience", "", "OE_MCP_OIDC_AUDIENCE"),
    ("allowed_emails", ("you@example.com",), "OE_MCP_ALLOWED_EMAILS"),
    ("oidc_required_scopes", ("work:create",), "OE_MCP_OIDC_REQUIRED_SCOPES"),
])
def test_oidc_settings_require_issuer(field, value, name):
    from dataclasses import replace

    with pytest.raises(ValueError, match=f"{name} requires OE_MCP_OIDC_ISSUER"):
        replace(SETTINGS, **{field: value})


@pytest.mark.parametrize("name,value", [
    ("OE_MCP_OIDC_AUDIENCE", SETTINGS.resource_url),
    ("OE_MCP_ALLOWED_EMAILS", "you@example.com"),
    ("OE_MCP_OIDC_REQUIRED_SCOPES", "work:create"),
    ("OE_MCP_OIDC_AUDIENCE", ""),
    ("OE_MCP_ALLOWED_EMAILS", ""),
    ("OE_MCP_OIDC_REQUIRED_SCOPES", ""),
])
def test_oidc_env_without_issuer_fails_startup(monkeypatch, tmp_path, capsys, name, value):
    from engine.apps.mcp_server.__main__ import main

    for variable in (
        "OE_MCP_OIDC_ISSUER", "OE_MCP_OIDC_AUDIENCE",
        "OE_MCP_ALLOWED_EMAILS", "OE_MCP_OIDC_REQUIRED_SCOPES",
    ):
        monkeypatch.delenv(variable, raising=False)
    for variable, setting in {
        "OE_MCP_TOKEN": TOKEN, "OE_MCP_REPOSITORY": "/repos/oe",
        "OE_MCP_WORKFLOW": "workflow", "OE_MCP_PUBLIC_URL": SETTINGS.public_url,
    }.items():
        monkeypatch.setenv(variable, setting)
    env_file = tmp_path / "mcp.env"
    env_file.write_text(f"{name}={value}\n")
    monkeypatch.setattr("sys.argv", ["engine-mcp-server", "--env-file", str(env_file)])
    def unexpected_run(*args, **kwargs):
        pytest.fail("Server must not start with orphaned OIDC settings")
    monkeypatch.setattr("engine.apps.mcp_server.__main__.uvicorn.run", unexpected_run)
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert f"{name} requires OE_MCP_OIDC_ISSUER" in capsys.readouterr().err
    monkeypatch.delenv(name)


# --- OE service token -----------------------------------------------------------

ENGINE_TOKEN = "engine-secret-" * 3


def test_engine_token_is_sent_to_oe_and_client_token_is_not():
    from dataclasses import replace
    requests = []

    def upstream(request):
        requests.append(request)
        return httpx.Response(201, json={"runId": "run-123", "phase": "working"})

    settings = replace(SETTINGS, engine_token=ENGINE_TOKEN)
    with TestClient(create_app(settings, transport=httpx.MockTransport(upstream))) as client:
        result = rpc(client, "tools/call", {
            "name": "create_workorder", "arguments": {"prompt": "Fix the bug"},
        }).json()["result"]
        assert result["structuredContent"] == {"run_id": "run-123"}
    assert requests[0].headers.get_list("authorization") == [f"Bearer {ENGINE_TOKEN}"]
    assert TOKEN not in str(requests[0].headers)


@pytest.mark.parametrize("engine_token", ["short", "has whitespace " * 3, TOKEN])
def test_invalid_engine_token_fails_closed(engine_token):
    with pytest.raises(ValueError, match="OE_MCP_ENGINE_TOKEN"):
        Settings(**{**SETTINGS.__dict__, "engine_token": engine_token})


def test_engine_token_is_not_in_repr():
    from dataclasses import replace
    assert ENGINE_TOKEN not in repr(replace(SETTINGS, engine_token=ENGINE_TOKEN))


@pytest.mark.parametrize("status, engine_token, expected", [
    ({"loginRequired": True}, "", "OE_MCP_ENGINE_TOKEN"),
    ({"loginRequired": True}, ENGINE_TOKEN, None),
    ({"loginRequired": False}, "", None),
    ("unreachable", "", None),
    ("not-json", "", None),
])
def test_engine_login_problem(status, engine_token, expected):
    from dataclasses import replace
    from engine.apps.mcp_server.server import engine_login_problem

    def upstream(request):
        assert request.url.path == "/api/auth/github/status"
        if status == "unreachable":
            raise httpx.ConnectError("refused", request=request)
        if status == "not-json":
            return httpx.Response(200, text="<html>")
        return httpx.Response(200, json={"authenticated": False, "user": None, **status})

    problem = engine_login_problem(
        replace(SETTINGS, engine_token=engine_token), transport=httpx.MockTransport(upstream)
    )
    assert problem == expected or (expected and expected in problem)


def test_login_required_without_engine_token_fails_startup(monkeypatch, tmp_path, capsys):
    from engine.apps.mcp_server import __main__ as entry

    for variable in (
        "OE_MCP_OIDC_ISSUER", "OE_MCP_OIDC_AUDIENCE", "OE_MCP_ALLOWED_EMAILS",
        "OE_MCP_OIDC_REQUIRED_SCOPES", "OE_MCP_ENGINE_TOKEN",
    ):
        monkeypatch.delenv(variable, raising=False)
    for variable, setting in {
        "OE_MCP_TOKEN": TOKEN, "OE_MCP_REPOSITORY": "/repos/oe",
        "OE_MCP_WORKFLOW": "workflow", "OE_MCP_PUBLIC_URL": SETTINGS.public_url,
    }.items():
        monkeypatch.setenv(variable, setting)
    env_file = tmp_path / "mcp.env"
    env_file.write_text("")
    monkeypatch.setattr("sys.argv", ["engine-mcp-server", "--env-file", str(env_file)])
    monkeypatch.setattr(entry, "engine_login_problem", lambda settings: "OE requires GitHub login")
    monkeypatch.setattr(entry.uvicorn, "run", lambda *a, **k: pytest.fail("server must not start"))
    with pytest.raises(SystemExit) as error:
        entry.main()
    assert error.value.code == 2
    assert "OE requires GitHub login" in capsys.readouterr().err
