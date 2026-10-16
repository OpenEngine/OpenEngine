"""The OAuth credential lifecycle events the web app emits.

The web surface stores a source control token in the OS keychain and refreshes
it in place when the provider rejects it with a 401. These tests pin the
non-secret lifecycle events that make those paths observable: a refresh that
rotates a token, a refresh the provider refuses, and an account that connects
or disconnects.
"""

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

from engine.apps.web.composition import _refresh_after_unauthorized
from engine.apps.web.github_auth import (
    DeviceFlowComplete,
    DeviceFlowState,
    GitHubAuthError,
    GitHubRefreshTokenInvalidError,
)
from engine.apps.web.oauth_credentials import StoredCredentials

_LOGGER = "engine.apps.web.oauth_lifecycle"

# Required by the repository lint rule against inline exception messages.
_REFUSED = "provider refused the refresh token"
_UNREACHABLE = "token endpoint is unreachable"
_KEYCHAIN_FAILED = "keychain refused the write"
_NO_REFRESH_TOKEN = "must not refresh without a refresh token"


def _high_priority_backend() -> MagicMock:
    backend = MagicMock()
    backend.priority = 5
    return backend


class _FakeStore:
    """A credential store double that records deletes and writes."""

    def __init__(self, credentials: StoredCredentials) -> None:
        self._credentials = credentials
        self.deleted = False
        self.saved: StoredCredentials | None = None

    @property
    def credential_identity(self) -> tuple[str, str]:
        return ("github-token", "user:1")

    def get_credentials(self) -> StoredCredentials:
        return self._credentials

    def set_credentials(self, credentials: StoredCredentials) -> None:
        self.saved = credentials

    def delete(self) -> None:
        self.deleted = True


def _refresh(
    store, refresh, *, client_id=lambda: "client", on_persist_failed=lambda: None
):
    return asyncio.run(
        _refresh_after_unauthorized(
            store,
            "old-token",
            client_id,
            refresh,
            GitHubRefreshTokenInvalidError,
            GitHubAuthError,
            provider="github",
            on_persist_failed=on_persist_failed,
        )
    )


def test_refresh_success_emits_started_and_succeeded(caplog) -> None:
    caplog.set_level(logging.INFO, logger=_LOGGER)
    store = _FakeStore(StoredCredentials("old-token", "refresh-1"))

    async def refresh(client_id, refresh_token):
        return StoredCredentials("new-token", "refresh-2")

    assert _refresh(store, refresh) is True
    assert store.saved is not None
    assert store.saved.access_token == "new-token"
    assert '"event": "github_refresh_started"' in caplog.text
    assert '"event": "github_refresh_succeeded"' in caplog.text
    # Only fingerprints reach the log, never the token material.
    assert "old-token" not in caplog.text
    assert "new-token" not in caplog.text


def test_refused_refresh_token_is_logged_and_credentials_deleted(caplog) -> None:
    caplog.set_level(logging.INFO, logger=_LOGGER)
    store = _FakeStore(StoredCredentials("old-token", "refresh-1"))

    async def refresh(client_id, refresh_token):
        raise GitHubRefreshTokenInvalidError(_REFUSED)

    assert _refresh(store, refresh) is False
    assert store.deleted is True
    assert '"event": "github_refresh_token_invalid"' in caplog.text
    assert "old-token" not in caplog.text


def test_refresh_transport_error_is_logged(caplog) -> None:
    caplog.set_level(logging.INFO, logger=_LOGGER)
    store = _FakeStore(StoredCredentials("old-token", "refresh-1"))

    async def refresh(client_id, refresh_token):
        raise GitHubAuthError(_UNREACHABLE)

    assert _refresh(store, refresh) is False
    assert store.deleted is False
    assert '"event": "github_refresh_failed"' in caplog.text
    assert "GitHubAuthError" in caplog.text


def test_unpersisted_refresh_is_logged(caplog) -> None:
    caplog.set_level(logging.INFO, logger=_LOGGER)
    store = _FakeStore(StoredCredentials("old-token", "refresh-1"))

    async def refresh(client_id, refresh_token):
        return StoredCredentials("new-token", "refresh-2")

    def set_credentials(credentials):
        raise GitHubAuthError(_KEYCHAIN_FAILED)

    store.set_credentials = set_credentials
    flagged: list[bool] = []
    assert (
        _refresh(store, refresh, on_persist_failed=lambda: flagged.append(True))
        is False
    )
    assert flagged == [True]
    assert '"event": "github_refresh_persist_failed"' in caplog.text
    assert "new-token" not in caplog.text


def test_missing_refresh_token_is_logged(caplog) -> None:
    caplog.set_level(logging.INFO, logger=_LOGGER)
    store = _FakeStore(StoredCredentials("old-token"))

    async def refresh(client_id, refresh_token):
        raise AssertionError(_NO_REFRESH_TOKEN)

    assert _refresh(store, refresh) is False
    assert '"event": "github_refresh_unavailable"' in caplog.text
    assert "no-refresh-token" in caplog.text


def test_connect_completion_emits_lifecycle_event(
    *, github_auth_app, client, caplog
) -> None:
    caplog.set_level(logging.INFO, logger=_LOGGER)
    app = github_auth_app()
    flow = DeviceFlowState(
        device_code="d",
        user_code="U",
        verification_uri="https://gh",
        expires_in=900,
        interval=5,
    )
    token = "token-that-must-not-appear-in-logs"
    with (
        patch(
            "engine.apps.web.api.start_device_flow", new=AsyncMock(return_value=flow)
        ),
        patch(
            "engine.apps.web.api.poll_device_flow",
            new=AsyncMock(return_value=DeviceFlowComplete(access_token=token)),
        ),
        patch(
            "engine.apps.web.oauth_credentials.keyring.get_keyring",
            return_value=_high_priority_backend(),
        ),
        patch("engine.apps.web.oauth_credentials.keyring.set_password"),
    ):
        with client(app) as browser:
            browser.post("/api/github/connect")
            resp = browser.post("/api/github/connect/poll")
    assert resp.json()["status"] == "complete"
    assert '"event": "github_connected"' in caplog.text
    assert token not in caplog.text


def test_disconnect_emits_lifecycle_event(*, github_auth_app, client, caplog) -> None:
    caplog.set_level(logging.INFO, logger=_LOGGER)
    app = github_auth_app()
    with client(
        app, base_url="https://localhost:8443", raise_server_exceptions=True
    ) as browser:
        resp = browser.post(
            "/api/github/disconnect", headers={"origin": "https://localhost:8443"}
        )
    assert resp.status_code == 204
    assert '"event": "github_disconnected"' in caplog.text
