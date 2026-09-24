"""Browser login issues session cookies after GitHub identity verification."""

import base64
import hashlib
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

from engine.apps.web.github_login import GitHubLogin, GitHubLoginConfig, RepositoryLoginPermission
from engine.adapters.source_control.github.transports import GitHubTransportError


@pytest.fixture
def permission_api(monkeypatch):
    request = AsyncMock(return_value={"permission": "write", "user": {"id": 42, "login": "alice"}})
    monkeypatch.setattr("engine.apps.web.github_login.GitHubCliTransport.request", request)
    return request


@pytest.fixture
def flow(permission_api):
    return GitHubLogin(GitHubLoginConfig(
        "login-client", "login-secret", "https://engine.test/api/auth/github/callback",
        repository="owner/repo",
    ))


def browser(flow):
    return TestClient(Starlette(routes=flow.routes()), base_url="https://engine.test")


def start(client):
    response = client.get("/api/auth/github/login", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["cache-control"] == "no-store"
    cookie = response.headers["set-cookie"]
    assert all(flag in cookie for flag in ("HttpOnly", "Secure", "SameSite=lax", "Max-Age=600"))
    params = parse_qs(urlsplit(response.headers["location"]).query)
    assert params["scope"] == ["read:user"]
    assert params["client_id"] == ["login-client"]
    assert params["code_challenge_method"] == ["S256"]
    return params


def callback(client, state, **params):
    return client.get("/api/auth/github/callback", params={"state": state, **params},
                      follow_redirects=False)


def _mock_provider(success_id=42, success_login="alice"):
    def provider(request):
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "private-token"})
        return httpx.Response(200, json={"id": success_id, "login": success_login, "email": "private"})
    return provider


def test_success_issues_session_and_redirects(flow):
    client = browser(flow)
    params = start(client)
    calls = []

    def provider(request):
        calls.append(request)
        if request.url.path == "/login/oauth/access_token":
            data = parse_qs(request.content.decode())
            assert data["client_secret"] == ["login-secret"]
            assert data["redirect_uri"] == params["redirect_uri"]
            assert data["code"] == ["one-use-code"]
            digest = hashlib.sha256(data["code_verifier"][0].encode()).digest()
            assert base64.urlsafe_b64encode(digest).decode().rstrip("=") == params["code_challenge"][0]
            return httpx.Response(200, json={"access_token": "private-token"})
        assert str(request.url) == "https://api.github.com/user"
        assert request.headers["Authorization"] == "Bearer private-token"
        return httpx.Response(200, json={"id": 42, "login": "alice", "email": "private"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
        response = callback(client, params["state"][0], code="one-use-code")
    assert response.status_code == 302
    assert response.headers["location"] == "/"
    assert response.headers["referrer-policy"] == "no-referrer"
    # Login cookie is cleared.
    cookies = response.headers.get_list("set-cookie")
    login_cookie = [c for c in cookies if "engine_github_login" in c]
    assert login_cookie and "Max-Age=0" in login_cookie[0]
    # Session cookie is set.
    session_cookie = [c for c in cookies if "engine_session" in c]
    assert session_cookie
    assert len(calls) == 2
    # Replay with consumed cookie is rejected.
    replay = callback(client, params["state"][0], code="one-use-code")
    assert replay.status_code == 302
    assert "error=" in replay.headers["location"]


@pytest.mark.parametrize("failure", ["missing", "wrong", "other-browser", "expired", "denied", "no-code"])
def test_rejects_invalid_callbacks_before_exchange(flow, failure, monkeypatch):
    client = browser(flow)
    state = start(client)["state"][0]
    params = {"code": "code"}
    if failure == "missing":
        state = ""
    elif failure == "wrong":
        state = "wrong"
    elif failure == "other-browser":
        client = browser(flow)
    elif failure == "expired":
        monkeypatch.setattr("engine.apps.web.github_login.time.time", lambda: 10**12)
    elif failure == "denied":
        params = {"error": "access_denied"}
    elif failure == "no-code":
        params = {}
    with patch("engine.apps.web.github_login.httpx.AsyncClient") as outbound:
        response = callback(client, state, **params)
        assert response.status_code == 302
        assert "/login?error=" in response.headers["location"]
        outbound.assert_not_called()


@pytest.mark.parametrize("failure", ["network", "http", "json", "error", "missing-token", "array", "bad-user", "user-http"])
def test_provider_failures_redirect_with_error(flow, failure):
    client = browser(flow)
    state = start(client)["state"][0]

    def provider(request):
        if failure == "network":
            raise httpx.ConnectError("secret", request=request)
        if failure == "http":
            return httpx.Response(500, text="secret")
        if failure == "json":
            return httpx.Response(200, text="secret")
        if failure == "error":
            return httpx.Response(200, json={"error": "secret"})
        if failure == "missing-token":
            return httpx.Response(200, json={})
        if failure == "array":
            return httpx.Response(200, json=[])
        if request.url.path == "/user":
            return httpx.Response(403 if failure == "user-http" else 200, json={"id": "42", "login": "alice"})
        return httpx.Response(200, json={"access_token": "secret"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
        response = callback(client, state, code="code")
    assert response.status_code == 302
    assert "/login?error=failed" in response.headers["location"]
    # Replay with consumed cookie is rejected.
    replay = callback(client, state, code="code")
    assert replay.status_code == 302
    assert "error=" in replay.headers["location"]


def test_disabled():
    client = browser(GitHubLogin(None))
    assert client.get("/api/auth/github/login").status_code == 503
    assert callback(client, "state", code="code").status_code == 503


@pytest.mark.parametrize("uri", ["http://public.test/api/auth/github/callback", "https://engine.test/wrong", "https://engine.test/api/auth/github/callback?next=evil", "https://user@engine.test/api/auth/github/callback"])
def test_rejects_unsafe_configuration(uri):
    with pytest.raises(ValueError):
        GitHubLoginConfig("client", "secret", uri, repository="owner/repo")


def test_loopback_and_secret_repr():
    config = GitHubLoginConfig("client", "private-secret", "http://127.0.0.1:8000/api/auth/github/callback",
    repository="owner/repo",
)
    assert "private-secret" not in repr(config)


def test_flood_does_not_block_independent_browser(flow):
    client = browser(flow)
    independent = browser(flow)
    state = start(independent)["state"][0]
    for _ in range(1100):
        client.cookies.clear()
        assert client.get("/api/auth/github/login", follow_redirects=False).status_code == 302
    assert browser(flow).get("/api/auth/github/login", follow_redirects=False).status_code == 302

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_provider()))
    with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
        assert callback(independent, state, code="code").status_code == 302


@pytest.mark.parametrize("cookie", ["garbage", "a.b.c.d", "x" * 257])
def test_invalid_cookie_rejected(flow, cookie):
    client = browser(flow)
    state = start(client)["state"][0]
    client.cookies.clear()
    with patch("engine.apps.web.github_login.httpx.AsyncClient") as outbound:
        response = client.get("/api/auth/github/callback", params={"state": state, "code": "code"},
                              headers={"cookie": f"engine_github_login={cookie}"},
                              follow_redirects=False)
        assert response.status_code == 302
        assert "error=" in response.headers["location"]
        outbound.assert_not_called()


def test_signed_cookie_tampering_rejected(flow):
    client = browser(flow)
    state = start(client)["state"][0]
    cookie = client.cookies.get("engine_github_login")
    parts = cookie.split(".")
    parts[2] = str(10**12)
    client.cookies.clear()
    with patch("engine.apps.web.github_login.httpx.AsyncClient") as outbound:
        response = client.get("/api/auth/github/callback", params={"state": state, "code": "code"},
                              headers={"cookie": "engine_github_login=" + ".".join(parts)},
                              follow_redirects=False)
        assert response.status_code == 302
        assert "error=" in response.headers["location"]
        outbound.assert_not_called()


def test_stale_callback_preserves_newer_login(flow):
    client = browser(flow)
    older = start(client)["state"][0]
    newer = start(client)["state"][0]
    response = callback(client, older, code="old")
    assert response.status_code == 302
    assert "error=" in response.headers["location"]
    # The login cookie for the newer flow should not be cleared.
    cookies = response.headers.get_list("set-cookie")
    login_cleared = [c for c in cookies if "engine_github_login" in c and "Max-Age=0" in c]
    assert not login_cleared
    # Refreshing a consumed callback must also leave the new cookie alone.
    replay_cookies = callback(client, older, code="old").headers.get_list("set-cookie")
    assert not [c for c in replay_cookies if "engine_github_login" in c and "Max-Age=0" in c]

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_provider()))
    with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
        response = callback(client, newer, code="new")
    assert response.status_code == 302
    assert response.headers["location"] == "/"
    cookies = response.headers.get_list("set-cookie")
    login_cleared = [c for c in cookies if "engine_github_login" in c and "Max-Age=0" in c]
    assert login_cleared


def test_token_exchange_uses_rotated_file_secret(tmp_path, monkeypatch, permission_api):
    monkeypatch.delenv("ENGINE_GITHUB_LOGIN_CLIENT_SECRET", raising=False)
    secret_file = tmp_path / ".env"
    secret_file.write_text("ENGINE_GITHUB_LOGIN_CLIENT_SECRET=initial\n")
    flow = GitHubLogin(GitHubLoginConfig(
        "login-client", "initial", "https://engine.test/api/auth/github/callback", secret_file,
        repository="owner/repo",
    ))
    client = browser(flow)
    state = start(client)["state"][0]
    secret_file.write_text("ENGINE_GITHUB_LOGIN_CLIENT_SECRET=rotated\n")

    def provider(request):
        if request.url.path == "/login/oauth/access_token":
            assert parse_qs(request.content.decode())["client_secret"] == ["rotated"]
            return httpx.Response(200, json={"access_token": "token"})
        return httpx.Response(200, json={"id": 42, "login": "alice"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
    with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
        assert callback(client, state, code="code").status_code == 302


def test_captured_callback_replay_cannot_verify_identity_twice(flow):
    client = browser(flow)
    state = start(client)["state"][0]
    cookie = client.cookies.get("engine_github_login")
    exchanges = 0
    identities = 0

    def provider(request):
        nonlocal exchanges, identities
        if request.url.path == "/login/oauth/access_token":
            exchanges += 1
            if exchanges > 1:
                return httpx.Response(200, json={"error": "bad_verification_code"})
            return httpx.Response(200, json={"access_token": "token"})
        identities += 1
        return httpx.Response(200, json={"id": 42, "login": "alice"})

    for expected in (302, 302):
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
        with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
            response = client.get(
                "/api/auth/github/callback", params={"state": state, "code": "same-code"},
                headers={"cookie": f"engine_github_login={cookie}"},
                follow_redirects=False,
            )
        assert response.status_code == expected
    assert identities == 1


def test_status_unauthenticated(flow):
    client = browser(flow)
    response = client.get("/api/auth/github/status")
    assert response.json() == {"authenticated": False, "user": None, "loginRequired": True}


def test_status_after_login(flow):
    client = browser(flow)
    params = start(client)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_provider()))
    with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
        callback(client, params["state"][0], code="code")
    response = client.get("/api/auth/github/status")
    data = response.json()
    assert data["authenticated"] is True
    assert data["user"]["id"] == 42
    assert data["user"]["login"] == "alice"


def test_logout_clears_session(flow):
    client = browser(flow)
    params = start(client)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_provider()))
    with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
        callback(client, params["state"][0], code="code")
    assert client.get("/api/auth/github/status").json()["authenticated"] is True
    response = client.post("/api/auth/github/logout")
    assert response.json() == {"ok": True}
    assert client.get("/api/auth/github/status").json()["authenticated"] is False


def test_logout_without_session_is_noop(flow):
    """Cross-site POST without a session cookie does not set Set-Cookie."""
    client = browser(flow)
    response = client.post("/api/auth/github/logout")
    assert response.json() == {"ok": True}
    assert "set-cookie" not in response.headers


def test_status_not_configured():
    flow = GitHubLogin(None)
    client = browser(flow)
    response = client.get("/api/auth/github/status")
    assert response.json() == {"authenticated": False, "user": None, "loginRequired": False}


# --- middleware tests ---------------------------------------------------------

def _app_with_middleware(flow):
    """Mount the real graph API alongside a web route behind session auth."""
    from engine.graph_runtime.api import create_app
    from graph_runtime_fakes import ScriptedGraphRuntime
    from starlette.responses import JSONResponse as _J
    routes = flow.routes() + [
        Route("/api/data", lambda _r: _J({"ok": True})),
        Mount("/graph", app=create_app(ScriptedGraphRuntime())),
    ]
    inner = Starlette(routes=routes)
    app = flow.middleware(inner)
    return TestClient(app, base_url="https://engine.test")


@pytest.mark.parametrize("path", ["/api/data", "/graph/api/graphs"])
def test_middleware_blocks_unauthenticated_api(flow, path):
    client = _app_with_middleware(flow)
    response = client.get(path)
    assert response.status_code == 401
    assert response.json()["error"] == "authentication required"


def test_middleware_allows_auth_endpoints(flow):
    client = _app_with_middleware(flow)
    # Status is exempt from the middleware.
    response = client.get("/api/auth/github/status")
    assert response.status_code == 200
    assert response.json()["loginRequired"] is True


@pytest.mark.parametrize("path, expected", [
    ("/api/data", {"ok": True}), ("/graph/api/graphs", {"graphs": []}),
])
def test_middleware_allows_authenticated_api(flow, path, expected):
    client = _app_with_middleware(flow)
    params = start(client)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_provider()))
    with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
        callback(client, params["state"][0], code="code")
    response = client.get(path)
    assert response.status_code == 200
    assert response.json() == expected


def test_middleware_noop_when_not_configured():
    flow = GitHubLogin(None)
    from starlette.responses import JSONResponse as _J
    routes = flow.routes() + [Route("/api/data", lambda _r: _J({"ok": True}))]
    inner = Starlette(routes=routes)
    app = flow.middleware(inner)
    client = TestClient(app, base_url="https://engine.test")
    response = client.get("/api/data")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.parametrize("failure", ["expired", "malformed", "tampered", "other-key"])
def test_invalid_session_rejected_at_status_and_api_boundaries(flow, failure):
    if failure == "expired":
        with patch("engine.apps.web.github_login.time.time", return_value=0):
            cookie = flow._make_session_cookie(42, "alice")
    elif failure == "malformed":
        cookie = "not-a-session"
    elif failure == "tampered":
        cookie = flow._make_session_cookie(42, "alice").replace("42|alice|", "43|admin|")
    else:
        cookie = GitHubLogin(flow.config)._make_session_cookie(42, "alice")
    client = _app_with_middleware(flow)
    headers = {"cookie": f"engine_session={cookie}"}
    status = client.get("/api/auth/github/status", headers=headers)
    assert status.status_code == 200
    assert status.json() == {"authenticated": False, "user": None, "loginRequired": True}
    for path in ("/api/data", "/graph/api/graphs"):
        response = client.get(path, headers=headers)
        assert response.status_code == 401
        assert response.json() == {"error": "authentication required"}


@pytest.mark.parametrize("destination, expected", [
    ("/runs/run-123?tab=events#latest", "/runs/run-123?tab=events#latest"),
    ("/conversations/thread-1", "/conversations/thread-1"),
    ("/projects/project%20with%20spaces/milestones?q=two%20words", "/projects/project%20with%20spaces/milestones?q=two%20words"),
    ("https://evil.test/", "/"), ("//evil.test/", "/"),
    ("/%2fevil.test", "/"), ("/\\evil.test", "/"),
    ("/%0d%0aLocation:evil", "/"), ("javascript:alert(1)", "/"),
])
def test_login_returns_to_validated_destination(flow, destination, expected):
    client = browser(flow)
    response = client.get("/api/auth/github/login", params={"return_to": destination},
                          follow_redirects=False)
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_provider()))
    with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
        response = callback(client, state, code="code", return_to="//evil.test")
    assert response.headers["location"] == expected
    assert client.get("/api/auth/github/status").json()["authenticated"] is True

@pytest.mark.parametrize("error", ["failed", "denied", "expired"])
def test_login_retry_preserves_destination(flow, error):
    client = browser(flow)
    destination = "/runs/run-123?tab=events#latest"
    response = client.get("/api/auth/github/login", params={"return_to": destination}, follow_redirects=False)
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    if error == "failed":
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
            response = callback(client, state, code="bad")
    elif error == "expired":
        cookie = client.cookies.get("engine_github_login")
        with patch("engine.apps.web.github_login.time.time", return_value=10**12):
            response = client.get(
                "/api/auth/github/callback", params={"state": state, "code": "old"},
                headers={"cookie": f"engine_github_login={cookie}"}, follow_redirects=False,
            )
    else:
        response = callback(client, state, error="access_denied")
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query == {"error": [error], "return_to": [destination]}
    response = client.get("/api/auth/github/login", params={"return_to": query["return_to"][0]}, follow_redirects=False)
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_provider()))
    with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
        response = callback(client, state, code="good")
    assert response.headers["location"] == destination


# --- service token ------------------------------------------------------------

SERVICE_TOKEN = "service-token-" * 3


def _service_app(token=SERVICE_TOKEN):
    from starlette.responses import JSONResponse as _J
    flow = GitHubLogin(GitHubLoginConfig(
        "login-client", "login-secret", "https://engine.test/api/auth/github/callback",
        repository="owner/repo",
    ), lambda: token)
    routes = flow.routes() + [
        Route("/api/runs", lambda _r: _J({"runId": "run-1"}, status_code=201), methods=["GET", "POST"]),
        Route("/api/runs/{run_id}", lambda _r: _J({"ok": True}), methods=["POST"]),
        Route("/api/data", lambda _r: _J({"ok": True}), methods=["GET", "POST"]),
    ]
    return TestClient(flow.middleware(Starlette(routes=routes)), base_url="https://engine.test")


def test_service_token_admits_run_creation():
    client = _service_app()
    response = client.post("/api/runs", json={}, headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
    assert response.status_code == 201


@pytest.mark.parametrize("method, path", [
    ("GET", "/api/runs"), ("POST", "/api/runs/run-1"), ("POST", "/api/data"), ("GET", "/api/data"),
])
def test_service_token_admits_nothing_else(method, path):
    client = _service_app()
    response = client.request(method, path, headers={"Authorization": f"Bearer {SERVICE_TOKEN}"})
    assert response.status_code == 401


@pytest.mark.parametrize("authorization", [
    None, "Bearer wrong-" + SERVICE_TOKEN, SERVICE_TOKEN, f"Basic {SERVICE_TOKEN}",
])
def test_service_token_rejects_missing_or_wrong_header(authorization):
    client = _service_app()
    headers = {"Authorization": authorization} if authorization else {}
    assert client.post("/api/runs", json={}, headers=headers).status_code == 401


@pytest.mark.parametrize("configured", ["", "short", "has whitespace " * 3])
def test_unset_or_invalid_service_token_admits_nothing(configured):
    client = _service_app(configured)
    response = client.post("/api/runs", json={}, headers={"Authorization": f"Bearer {configured}"})
    assert response.status_code == 401


def test_service_token_rotation_takes_effect_per_request():
    current = {"token": SERVICE_TOKEN}
    from starlette.responses import JSONResponse as _J
    flow = GitHubLogin(GitHubLoginConfig(
        "login-client", "login-secret", "https://engine.test/api/auth/github/callback",
        repository="owner/repo",
    ), lambda: current["token"])
    routes = [Route("/api/runs", lambda _r: _J({}, status_code=201), methods=["POST"])]
    client = TestClient(flow.middleware(Starlette(routes=routes)), base_url="https://engine.test")
    assert client.post("/api/runs", headers={"Authorization": f"Bearer {SERVICE_TOKEN}"}).status_code == 201
    current["token"] = "rotated-token-" * 3
    assert client.post("/api/runs", headers={"Authorization": f"Bearer {SERVICE_TOKEN}"}).status_code == 401
    assert client.post("/api/runs", headers={"Authorization": f"Bearer {current['token']}"}).status_code == 201


def test_service_token_reader_prefers_environment_and_rereads_file(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from engine.apps.web.__main__ import _service_token_reader

    monkeypatch.delenv("ENGINE_SERVICE_TOKEN", raising=False)
    loaded = SimpleNamespace(path=tmp_path / "engine.toml")
    env = tmp_path / ".env"
    env.write_text(f"ENGINE_SERVICE_TOKEN={SERVICE_TOKEN}\n")
    read = _service_token_reader(loaded)
    assert read() == SERVICE_TOKEN
    env.write_text("ENGINE_SERVICE_TOKEN=rotated-token-rotated-token-rotated\n")
    assert read() == "rotated-token-rotated-token-rotated"
    monkeypatch.setenv("ENGINE_SERVICE_TOKEN", "from-environment-" * 2)
    assert read() == "from-environment-" * 2


def test_invalid_service_token_fails_startup(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from engine.apps.web.__main__ import _service_token_reader
    from engine.runtime import EngineConfigError

    monkeypatch.setenv("ENGINE_SERVICE_TOKEN", "short")
    with pytest.raises(EngineConfigError, match="ENGINE_SERVICE_TOKEN"):
        _service_token_reader(SimpleNamespace(path=tmp_path / "engine.toml"))


@pytest.mark.parametrize("permission, admitted", [
    ("write", True), ("maintain", True), ("admin", True),
    ("read", False), ("triage", False), ("none", False), ("unknown", False),
])
def test_callback_requires_repository_permission(flow, permission_api, permission, admitted):
    permission_api.return_value["permission"] = permission
    client = browser(flow)
    state = start(client)["state"][0]
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_provider()))
    with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
        response = callback(client, state, code="code")
    assert response.headers["location"] == ("/" if admitted else "/login?error=forbidden")
    assert bool(client.cookies.get("engine_session")) is admitted
    assert client.get("/api/auth/github/status").json()["authenticated"] is admitted
    permission_api.assert_awaited_once_with("GET", "/repos/owner/repo/collaborators/alice/permission")


@pytest.mark.parametrize("body", [
    None, [], {}, {"permission": "write"},
    {"permission": "write", "user": {"id": 99, "login": "alice"}},
    {"permission": "write", "user": {"id": 42, "login": "other"}},
    {"permission": "write", "user": {"id": "42", "login": "alice"}},
    {"permission": ["write"], "user": {"id": 42, "login": "alice"}},
])
def test_unexpected_permission_response_denies_login(flow, permission_api, body):
    permission_api.return_value = body
    client = browser(flow)
    state = start(client)["state"][0]
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_provider()))
    with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
        response = callback(client, state, code="code")
    assert response.headers["location"] == "/login?error=forbidden"
    assert not client.cookies.get("engine_session")


@pytest.mark.parametrize("error", ["404 Not Found", "403 Forbidden", "500 Server Error", "timeout", "invalid JSON"])
def test_permission_api_errors_deny_login(flow, permission_api, error):
    permission_api.side_effect = GitHubTransportError(error)
    client = browser(flow)
    state = start(client)["state"][0]
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_mock_provider()))
    with patch("engine.apps.web.github_login.httpx.AsyncClient", return_value=http_client):
        response = callback(client, state, code="code")
    assert response.headers["location"] == "/login?error=forbidden"
    assert not client.cookies.get("engine_session")


@pytest.mark.parametrize("failure", ["revoked", "api-error", "renamed"])
def test_existing_session_rechecks_permission(flow, permission_api, failure):
    client = _app_with_middleware(flow)
    now = [1000.0]
    with patch("engine.apps.web.github_login.time.monotonic", side_effect=lambda: now[0]):
        # Two browsers for the same user share one permission check.
        client.cookies.set("engine_session", flow._make_session_cookie(42, "alice"))
        assert client.get("/api/data").status_code == 200
        other = browser(flow)
        other.cookies.set("engine_session", flow._make_session_cookie(42, "alice"))
        assert other.get("/api/auth/github/status").json()["authenticated"]
        if failure == "revoked":
            permission_api.return_value["permission"] = "read"
        elif failure == "renamed":
            permission_api.return_value["user"]["id"] = 99
        else:
            permission_api.side_effect = GitHubTransportError("unavailable")
        now[0] += 899
        assert client.get("/api/data").status_code == 200
        assert permission_api.await_count == 1
        now[0] += 1
        assert client.get("/api/data").status_code == 401
        assert client.get("/graph/api/graphs").status_code == 401
        assert not other.get("/api/auth/github/status").json()["authenticated"]
        assert permission_api.await_count == (4 if failure == "api-error" else 2)


@pytest.mark.parametrize("error", [
    GitHubTransportError("timeout"), GitHubTransportError("HTTP 429"),
    OSError("network unavailable"), ValueError("invalid JSON"),
])
@pytest.mark.parametrize("expired", [False, True])
def test_permission_lookup_error_allows_retry(permission_api, error, expired):
    import asyncio

    async def scenario():
        permission = RepositoryLoginPermission("owner/repo")
        if expired:
            assert await permission.allowed(42, "alice")
            permission._cache[(42, "alice")] = (0, True)
        permission_api.reset_mock()
        permission_api.side_effect = [error, permission_api.return_value]
        assert not await permission.allowed(42, "alice")
        assert await permission.allowed(42, "alice")
        assert await permission.allowed(42, "alice")
        assert permission_api.await_count == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("body", [
    {"permission": "none"}, {"permission": "read"},
    {"permission": "write", "user": {"id": 99, "login": "alice"}},
])
def test_real_permission_denials_remain_cached(permission_api, body):
    import asyncio

    async def scenario():
        permission = RepositoryLoginPermission("owner/repo")
        permission_api.return_value = body
        assert not await permission.allowed(42, "alice")
        permission_api.return_value = {"permission": "write", "user": {"id": 42, "login": "alice"}}
        assert not await permission.allowed(42, "alice")
        permission_api.assert_awaited_once()

    asyncio.run(scenario())


def test_permission_cache_is_per_identity(permission_api):
    import asyncio

    async def scenario():
        permission = RepositoryLoginPermission("owner/repo")
        assert await permission.allowed(42, "alice")
        assert not await permission.allowed(43, "bob")
        assert await permission.allowed(42, "alice")
        assert permission_api.await_count == 2
        assert not await RepositoryLoginPermission("").allowed(42, "alice")
        assert permission_api.await_count == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("cached", [False, True])
def test_slow_permission_lookup_does_not_block_other_identities(permission_api, cached):
    import asyncio

    async def scenario():
        permission = RepositoryLoginPermission("owner/repo")
        started = asyncio.Event()
        release = asyncio.Event()

        async def request(method, path):
            login = path.split("/")[-2]
            if login == "bob":
                started.set()
                await release.wait()
            return {"permission": "write", "user": {
                "id": 42 if login == "alice" else 43, "login": login,
            }}

        permission_api.side_effect = request
        if cached:
            assert await permission.allowed(42, "alice")
        slow = asyncio.create_task(permission.allowed(43, "bob"))
        duplicate = None
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            duplicate = asyncio.create_task(permission.allowed(43, "bob"))
            assert await asyncio.wait_for(permission.allowed(42, "alice"), timeout=1)
            assert not slow.done()
            assert not duplicate.done()
        finally:
            release.set()
            results = await asyncio.gather(slow, *([duplicate] if duplicate else []))
        assert all(results)
        # Same-identity requests still share a lookup, and idle locks are released.
        assert permission_api.await_count == 2
        assert not permission._locks

    asyncio.run(scenario())


def test_permission_uses_host_cli_identity(monkeypatch):
    import asyncio
    import json

    monkeypatch.setenv("GITHUB_TOKEN", "settings-token")
    monkeypatch.setenv("GITHUB_ENTERPRISE_TOKEN", "settings-enterprise-token")
    monkeypatch.setenv("GH_HOST", "other.example")
    process = AsyncMock()
    process.returncode = 0
    process.communicate.return_value = (
        json.dumps({"permission": "write", "user": {"id": 42, "login": "alice"}}).encode(), b""
    )
    with patch("asyncio.create_subprocess_exec", return_value=process) as spawn:
        assert asyncio.run(RepositoryLoginPermission("owner/repo").allowed(42, "alice"))
    args, kwargs = spawn.call_args
    assert args[:3] == ("gh", "api", "/repos/owner/repo/collaborators/alice/permission")
    assert args[args.index("--hostname") + 1] == "github.com"
    assert "GITHUB_TOKEN" not in kwargs["env"]
    assert "GITHUB_ENTERPRISE_TOKEN" not in kwargs["env"]


def test_service_token_does_not_require_repository_check(monkeypatch):
    check = AsyncMock(side_effect=AssertionError("service tokens must bypass the check"))
    monkeypatch.setattr(RepositoryLoginPermission, "allowed", check)
    client = _service_app()
    client.cookies.set("engine_session", "invalid-browser-session")
    assert client.post("/api/runs", headers={"Authorization": f"Bearer {SERVICE_TOKEN}"}).status_code == 201
    check.assert_not_awaited()
