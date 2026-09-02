"""Tests for the GitHub OAuth device flow and credential store."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import keyring
import keyring.backend
import keyring.backends.fail
import pytest

from engine.apps.web.github_auth import (
    DeviceFlowComplete,
    DeviceFlowPending,
    DeviceFlowState,
    GitHubAuthError,
    GitHubCredentialStore,
    poll_device_flow,
    start_device_flow,
)


# ---------------------------------------------------------------------------
# GitHubCredentialStore
# ---------------------------------------------------------------------------


class TestGitHubCredentialStore:
    def test_get_returns_none_when_nothing_stored(self, monkeypatch):
        monkeypatch.setattr(keyring, "get_password", lambda *_: None)
        assert GitHubCredentialStore().get() is None

    def test_get_returns_stored_token(self, monkeypatch):
        monkeypatch.setattr(keyring, "get_password", lambda *_: "tok-abc")
        assert GitHubCredentialStore().get() == "tok-abc"

    def test_set_writes_to_keyring(self, monkeypatch):
        written: list[tuple] = []
        real_backend = MagicMock()
        real_backend.priority = 5
        monkeypatch.setattr(keyring, "get_keyring", lambda: real_backend)
        monkeypatch.setattr(
            keyring,
            "set_password",
            lambda service, username, password: written.append(
                (service, username, password)
            ),
        )
        GitHubCredentialStore().set("tok-xyz")
        assert written == [("openengine", "github-token", "tok-xyz")]

    def test_set_raises_when_no_secure_backend(self, monkeypatch):
        monkeypatch.setattr(
            keyring, "get_keyring", lambda: keyring.backends.fail.Keyring()
        )
        with pytest.raises(GitHubAuthError, match="no secure keyring"):
            GitHubCredentialStore().set("tok-xyz")

    def test_set_raises_for_any_low_priority_backend(self, monkeypatch):
        """Priority < 1 means stub/null backend, not just the fail class."""
        stub = MagicMock()
        stub.priority = 0
        monkeypatch.setattr(keyring, "get_keyring", lambda: stub)
        with pytest.raises(GitHubAuthError, match="no secure keyring"):
            GitHubCredentialStore().set("tok")

    def test_delete_swallows_missing_password_error(self, monkeypatch):
        def raise_missing(*_):
            raise keyring.errors.PasswordDeleteError("not found")

        monkeypatch.setattr(keyring, "delete_password", raise_missing)
        GitHubCredentialStore().delete()  # must not raise


# ---------------------------------------------------------------------------
# start_device_flow
# ---------------------------------------------------------------------------


def _mock_response(status_code: int, body: dict) -> httpx.Response:
    return httpx.Response(
        status_code, json=body, request=httpx.Request("POST", "https://x")
    )


class TestStartDeviceFlow:
    def test_returns_state_on_success(self, monkeypatch):
        body = {
            "device_code": "dev-1",
            "user_code": "ABCD-EFGH",
            "verification_uri": "https://github.com/login/device",
            "expires_in": 900,
            "interval": 5,
        }
        monkeypatch.setattr(
            "engine.apps.web.github_auth.httpx.AsyncClient",
            lambda: _client_returning(_mock_response(200, body)),
        )
        state = asyncio.run(start_device_flow("client-id"))
        assert state.device_code == "dev-1"
        assert state.user_code == "ABCD-EFGH"
        assert state.expires_in == 900
        assert state.interval == 5

    def test_raises_on_http_error(self, monkeypatch):
        monkeypatch.setattr(
            "engine.apps.web.github_auth.httpx.AsyncClient",
            lambda: _client_returning(
                _mock_response(401, {"message": "Bad credentials"})
            ),
        )
        with pytest.raises(GitHubAuthError, match="401"):
            asyncio.run(start_device_flow("client-id"))

    def test_raises_on_error_field(self, monkeypatch):
        monkeypatch.setattr(
            "engine.apps.web.github_auth.httpx.AsyncClient",
            lambda: _client_returning(_mock_response(200, {"error": "not_found"})),
        )
        with pytest.raises(GitHubAuthError, match="not_found"):
            asyncio.run(start_device_flow("client-id"))


# ---------------------------------------------------------------------------
# poll_device_flow
# ---------------------------------------------------------------------------


class TestPollDeviceFlow:
    def test_returns_complete_with_token(self, monkeypatch):
        monkeypatch.setattr(
            "engine.apps.web.github_auth.httpx.AsyncClient",
            lambda: _client_returning(
                _mock_response(200, {"access_token": "ghs_secret"})
            ),
        )
        result = asyncio.run(poll_device_flow("cid", "dev-1", current_interval=5))
        assert isinstance(result, DeviceFlowComplete)
        assert result.access_token == "ghs_secret"

    def test_returns_pending_for_authorization_pending(self, monkeypatch):
        monkeypatch.setattr(
            "engine.apps.web.github_auth.httpx.AsyncClient",
            lambda: _client_returning(
                _mock_response(200, {"error": "authorization_pending"})
            ),
        )
        result = asyncio.run(poll_device_flow("cid", "dev-1", current_interval=5))
        assert isinstance(result, DeviceFlowPending)
        assert result.next_interval == 5  # unchanged

    def test_slow_down_increases_interval_by_five(self, monkeypatch):
        monkeypatch.setattr(
            "engine.apps.web.github_auth.httpx.AsyncClient",
            lambda: _client_returning(_mock_response(200, {"error": "slow_down"})),
        )
        result = asyncio.run(poll_device_flow("cid", "dev-1", current_interval=5))
        assert isinstance(result, DeviceFlowPending)
        assert result.next_interval == 10  # 5 + 5 penalty

    def test_slow_down_accumulates_on_repeated_calls(self, monkeypatch):
        """Each slow_down adds 5 s to whatever the caller passes as current_interval."""
        monkeypatch.setattr(
            "engine.apps.web.github_auth.httpx.AsyncClient",
            lambda: _client_returning(_mock_response(200, {"error": "slow_down"})),
        )
        first = asyncio.run(poll_device_flow("cid", "dev-1", current_interval=5))
        assert isinstance(first, DeviceFlowPending)
        second = asyncio.run(
            poll_device_flow("cid", "dev-1", current_interval=first.next_interval)
        )
        assert isinstance(second, DeviceFlowPending)
        assert second.next_interval == 15

    def test_raises_on_expired(self, monkeypatch):
        monkeypatch.setattr(
            "engine.apps.web.github_auth.httpx.AsyncClient",
            lambda: _client_returning(_mock_response(200, {"error": "expired_token"})),
        )
        with pytest.raises(GitHubAuthError, match="expired_token"):
            asyncio.run(poll_device_flow("cid", "dev-1", current_interval=5))

    def test_raises_on_access_denied(self, monkeypatch):
        monkeypatch.setattr(
            "engine.apps.web.github_auth.httpx.AsyncClient",
            lambda: _client_returning(_mock_response(200, {"error": "access_denied"})),
        )
        with pytest.raises(GitHubAuthError, match="access_denied"):
            asyncio.run(poll_device_flow("cid", "dev-1", current_interval=5))

    def test_raises_on_http_error(self, monkeypatch):
        monkeypatch.setattr(
            "engine.apps.web.github_auth.httpx.AsyncClient",
            lambda: _client_returning(_mock_response(503, {})),
        )
        with pytest.raises(GitHubAuthError, match="503"):
            asyncio.run(poll_device_flow("cid", "dev-1", current_interval=5))

    def test_raises_when_token_absent(self, monkeypatch):
        monkeypatch.setattr(
            "engine.apps.web.github_auth.httpx.AsyncClient",
            lambda: _client_returning(_mock_response(200, {})),
        )
        with pytest.raises(GitHubAuthError, match="no access_token"):
            asyncio.run(poll_device_flow("cid", "dev-1", current_interval=5))


# ---------------------------------------------------------------------------
# CSRF guard (_is_local_request) via the API endpoints
# ---------------------------------------------------------------------------


def _make_github_app(tmp_path, client_id: str = "test-client-id"):
    """Minimal app wired with stub capabilities (only GitHub auth endpoints under test)."""
    from engine.adapters.state_store.sqlite import SQLiteStateStore
    from engine.apps.web.api import create_app
    from engine.apps.web.github_auth import GitHubCredentialStore
    from engine.apps.web.source_control import SourceControlPreferences
    from engine.runtime import AgentSession, Capabilities

    _stub = object()
    store = SQLiteStateStore(str(tmp_path / "t.sqlite3"))
    caps = Capabilities(
        workflow_runtime=_stub,
        source_control=_stub,
        agent_runner=_stub,
        communications=_stub,
        workspace_provider=_stub,
        state_store=store,
    )
    # WorkflowExecutor validates runners ⊆ review_runners; pass a matching pair.
    _runner_stub = {"default": _stub}
    session = AgentSession(caps, profiles={}, runners=_runner_stub)
    credential_store = GitHubCredentialStore()
    app = create_app(
        session,
        _runner_stub,
        workflow_runners=_runner_stub,
        review_runners=_runner_stub,
        credential_store=credential_store,
        github_client_id=client_id,
        source_control_preferences=SourceControlPreferences(tmp_path / "settings.json"),
    )
    return app


class TestCsrfGuard:
    """Mutating GitHub endpoints must reject cross-origin requests."""

    def _post(self, app, path: str, origin: str | None = None):
        from starlette.testclient import TestClient

        headers = {"origin": origin} if origin else {}
        with TestClient(app, raise_server_exceptions=True) as client:
            return client.post(path, headers=headers)

    def test_connect_from_localhost_origin_is_allowed(self, tmp_path):
        app = _make_github_app(tmp_path)
        with patch(
            "engine.apps.web.api.start_device_flow",
            new=AsyncMock(
                return_value=DeviceFlowState(
                    device_code="d",
                    user_code="U",
                    verification_uri="https://gh",
                    expires_in=900,
                    interval=5,
                )
            ),
        ):
            resp = self._post(
                app, "/api/github/connect", origin="http://localhost:8000"
            )
        assert resp.status_code != 403

    def test_connect_from_cross_origin_is_rejected(self, tmp_path):
        app = _make_github_app(tmp_path)
        resp = self._post(app, "/api/github/connect", origin="https://evil.example.com")
        assert resp.status_code == 403

    def test_connect_from_lookalike_localhost_origin_is_rejected(self, tmp_path):
        app = _make_github_app(tmp_path)
        resp = self._post(
            app, "/api/github/connect", origin="https://localhost.evil.example.com"
        )
        assert resp.status_code == 403

    def test_disconnect_from_cross_origin_is_rejected(self, tmp_path):
        app = _make_github_app(tmp_path)
        resp = self._post(
            app, "/api/github/disconnect", origin="https://evil.example.com"
        )
        assert resp.status_code == 403

    def test_disconnect_from_https_localhost_is_allowed(self, tmp_path):
        app = _make_github_app(tmp_path)
        resp = self._post(
            app, "/api/github/disconnect", origin="https://localhost:8443"
        )
        assert resp.status_code == 204

    def test_status_is_exempt_from_csrf_guard(self, tmp_path):
        from starlette.testclient import TestClient

        app = _make_github_app(tmp_path)
        with TestClient(app) as client:
            resp = client.get(
                "/api/github/status", headers={"origin": "https://evil.example.com"}
            )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# poll endpoint returns nextInterval only when pending
# ---------------------------------------------------------------------------


class TestPollEndpoint:
    def test_complete_response_has_no_next_interval(self, tmp_path):
        from starlette.testclient import TestClient

        app = _make_github_app(tmp_path)
        flow = DeviceFlowState(
            device_code="d",
            user_code="U",
            verification_uri="https://gh",
            expires_in=900,
            interval=5,
        )
        with (
            patch(
                "engine.apps.web.api.start_device_flow",
                new=AsyncMock(return_value=flow),
            ),
            patch(
                "engine.apps.web.api.poll_device_flow",
                new=AsyncMock(return_value=DeviceFlowComplete(access_token="tok")),
            ),
            patch(
                "engine.apps.web.github_auth.keyring.get_keyring",
                return_value=_high_priority_backend(),
            ),
            patch("engine.apps.web.github_auth.keyring.set_password"),
        ):
            with TestClient(app) as client:
                client.post("/api/github/connect")
                resp = client.post("/api/github/connect/poll")
        body = resp.json()
        assert body["status"] == "complete"
        assert "nextInterval" not in body

    def test_pending_response_carries_next_interval(self, tmp_path):
        from starlette.testclient import TestClient

        app = _make_github_app(tmp_path)
        flow = DeviceFlowState(
            device_code="d",
            user_code="U",
            verification_uri="https://gh",
            expires_in=900,
            interval=5,
        )
        with (
            patch(
                "engine.apps.web.api.start_device_flow",
                new=AsyncMock(return_value=flow),
            ),
            patch(
                "engine.apps.web.api.poll_device_flow",
                new=AsyncMock(return_value=DeviceFlowPending(next_interval=10)),
            ),
        ):
            with TestClient(app) as client:
                client.post("/api/github/connect")
                resp = client.post("/api/github/connect/poll")
        body = resp.json()
        assert body["status"] == "pending"
        assert body["nextInterval"] == 10


class TestSourceControlProviderEndpoint:
    def test_selects_provider_and_rejects_gitlab(self, tmp_path, monkeypatch) -> None:
        from starlette.testclient import TestClient

        from engine.apps.web.source_control import GhCliStatus

        monkeypatch.setattr(
            "engine.apps.web.source_control.gh_cli_status",
            lambda: GhCliStatus(True, True, account="octocat"),
        )
        app = _make_github_app(tmp_path)
        with TestClient(app) as client:
            status = client.get("/api/source-control/status")
            selected = client.post(
                "/api/source-control/provider", json={"provider": "github-oauth"}
            )
            rejected = client.post(
                "/api/source-control/provider", json={"provider": "gitlab"}
            )

        assert status.json()["provider"] == "gh-cli"
        assert status.json()["ghCli"]["account"] == "octocat"
        assert selected.status_code == 204
        assert rejected.status_code == 409


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _high_priority_backend():
    backend = MagicMock()
    backend.priority = 5
    return backend


class _AsyncContextManager:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def post(self, *_args, **_kwargs):
        return self._response


def _client_returning(response: httpx.Response) -> _AsyncContextManager:
    return _AsyncContextManager(response)
