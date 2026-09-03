from unittest.mock import AsyncMock, MagicMock, patch

import keyring
import pytest

from starlette.testclient import TestClient

from engine.apps.web.slack_auth import (
    SlackAuthError,
    SlackCommunications,
    SlackCredentialStore,
    SlackCredentials,
    authorization_url,
)


def test_slack_communications_posts_to_requested_channel() -> None:
    store = MagicMock(spec=SlackCredentialStore)
    store.token.return_value = "xoxb-token"
    response = MagicMock(is_error=False)
    response.json.return_value = {"ok": True, "ts": "123.456"}

    with patch("engine.apps.web.slack_auth.httpx.AsyncClient") as client_type:
        client_type.return_value.__aenter__.return_value.post = AsyncMock(
            return_value=response
        )
        message_id = __import__("asyncio").run(
            SlackCommunications(store).post("OpenEngine", "Review run-42")
        )

    assert message_id == "123.456"
    client_type.return_value.__aenter__.return_value.post.assert_awaited_once_with(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": "Bearer xoxb-token"},
        json={"channel": "OpenEngine", "text": "Review run-42"},
    )


def test_authorization_url_requests_notification_scope_and_state() -> None:
    url = authorization_url("123", "http://localhost/api/slack/callback", "nonce")
    assert url.startswith("https://slack.com/oauth/v2/authorize?")
    assert "scope=chat%3Awrite" in url
    assert "state=nonce" in url


def test_credentials_are_restored_when_secret_write_fails() -> None:
    values = {"slack-client-id": "old-id", "slack-client-secret": "old-secret"}

    def set_password(_service: str, username: str, value: str) -> None:
        if username == "slack-client-secret" and value == "new-secret":
            raise keyring.errors.PasswordSetError("failed")
        values[username] = value

    with (
        patch("engine.apps.web.slack_auth.keyring.get_keyring", return_value=MagicMock(priority=1)),
        patch("engine.apps.web.slack_auth.keyring.get_password", side_effect=lambda _s, u: values.get(u)),
        patch("engine.apps.web.slack_auth.keyring.set_password", side_effect=set_password),
        patch("engine.apps.web.slack_auth.keyring.delete_password"),
    ):
        with pytest.raises(SlackAuthError):
            SlackCredentialStore().set_credentials("new-id", "new-secret")

    assert values == {"slack-client-id": "old-id", "slack-client-secret": "old-secret"}


def test_slack_oauth_endpoints_complete_connection(tmp_path) -> None:
    from engine.adapters.state_store.sqlite import SQLiteStateStore
    from engine.apps.web.api import create_app
    from engine.runtime import AgentSession, Capabilities

    stub = object()
    capabilities = Capabilities(
        workflow_runtime=stub,
        source_control=stub,
        agent_runner=stub,
        communications=stub,
        workspace_provider=stub,
        state_store=SQLiteStateStore(str(tmp_path / "state.sqlite3")),
    )
    runners = {"default": stub}
    session = AgentSession(capabilities, profiles={}, runners=runners)
    slack_store = MagicMock(spec=SlackCredentialStore)
    slack_store.credentials.return_value = SlackCredentials("client", "secret")
    slack_store.token.side_effect = [None, "xoxb-token"]
    app = create_app(
        session,
        runners,
        workflow_runners=runners,
        review_runners=runners,
        workflow_catalog=MagicMock(),
        slack_credential_store=slack_store,
    )

    with (
        patch("engine.apps.web.api.uuid4", return_value=MagicMock(hex="nonce")),
        patch("engine.apps.web.api.exchange_slack_code", new=AsyncMock(return_value="xoxb-token")),
        TestClient(app) as client,
    ):
        before = client.get("/api/slack/status")
        connect = client.post("/api/slack/connect")
        callback = client.get("/api/slack/callback?code=code&state=nonce")
        after = client.get("/api/slack/status")

    assert before.json() == {"configured": True, "connected": False}
    assert "client_id=client" in connect.json()["authorizationUrl"]
    assert callback.status_code == 200
    slack_store.set_token.assert_called_once_with("xoxb-token")
    assert after.json() == {"configured": True, "connected": True}


def test_slack_callback_rejects_wrong_state(tmp_path) -> None:
    from engine.adapters.state_store.sqlite import SQLiteStateStore
    from engine.apps.web.api import create_app
    from engine.runtime import AgentSession, Capabilities

    stub = object()
    capabilities = Capabilities(stub, stub, stub, stub, stub, SQLiteStateStore(str(tmp_path / "s.sqlite3")))
    runners = {"default": stub}
    store = MagicMock(spec=SlackCredentialStore)
    store.credentials.return_value = SlackCredentials("client", "secret")
    app = create_app(AgentSession(capabilities, profiles={}, runners=runners), runners,
                     workflow_runners=runners, review_runners=runners,
                     workflow_catalog=MagicMock(), slack_credential_store=store)
    with TestClient(app) as client:
        client.post("/api/slack/connect")
        response = client.get("/api/slack/callback?code=code&state=wrong")
    assert response.status_code == 400
    store.set_token.assert_not_called()


def test_slack_disconnect_revokes_before_forgetting_token(tmp_path) -> None:
    from engine.adapters.state_store.sqlite import SQLiteStateStore
    from engine.apps.web.api import create_app
    from engine.runtime import AgentSession, Capabilities

    stub = object()
    capabilities = Capabilities(stub, stub, stub, stub, stub, SQLiteStateStore(str(tmp_path / "s.sqlite3")))
    runners = {"default": stub}
    store = MagicMock(spec=SlackCredentialStore)
    store.token.return_value = "xoxb-token"
    app = create_app(AgentSession(capabilities, profiles={}, runners=runners), runners,
                     workflow_runners=runners, review_runners=runners,
                     workflow_catalog=MagicMock(), slack_credential_store=store)
    revoke = AsyncMock()

    with patch("engine.apps.web.api.revoke_slack_token", new=revoke), TestClient(app) as client:
        response = client.post("/api/slack/disconnect")

    assert response.status_code == 204
    revoke.assert_awaited_once_with("xoxb-token")
    store.disconnect.assert_called_once_with()


def test_slack_disconnect_preserves_token_when_revocation_fails(tmp_path) -> None:
    from engine.adapters.state_store.sqlite import SQLiteStateStore
    from engine.apps.web.api import create_app
    from engine.runtime import AgentSession, Capabilities

    stub = object()
    capabilities = Capabilities(stub, stub, stub, stub, stub, SQLiteStateStore(str(tmp_path / "s.sqlite3")))
    runners = {"default": stub}
    store = MagicMock(spec=SlackCredentialStore)
    store.token.return_value = "xoxb-token"
    app = create_app(AgentSession(capabilities, profiles={}, runners=runners), runners,
                     workflow_runners=runners, review_runners=runners,
                     workflow_catalog=MagicMock(), slack_credential_store=store)
    revoke = AsyncMock(side_effect=SlackAuthError("Slack unavailable"))

    with patch("engine.apps.web.api.revoke_slack_token", new=revoke), TestClient(app) as client:
        response = client.post("/api/slack/disconnect")

    assert response.status_code == 502
    store.disconnect.assert_not_called()


def test_changing_credentials_revokes_existing_token_first(tmp_path) -> None:
    from engine.adapters.state_store.sqlite import SQLiteStateStore
    from engine.apps.web.api import create_app
    from engine.runtime import AgentSession, Capabilities

    stub = object()
    capabilities = Capabilities(stub, stub, stub, stub, stub, SQLiteStateStore(str(tmp_path / "s.sqlite3")))
    runners = {"default": stub}
    store = MagicMock(spec=SlackCredentialStore)
    store.token.return_value = "xoxb-old-token"
    app = create_app(AgentSession(capabilities, profiles={}, runners=runners), runners,
                     workflow_runners=runners, review_runners=runners,
                     workflow_catalog=MagicMock(), slack_credential_store=store)
    events: list[str] = []
    revoke = AsyncMock(side_effect=lambda _token: events.append("revoke"))
    store.disconnect.side_effect = lambda: events.append("disconnect")
    store.set_credentials.side_effect = lambda *_args: events.append("save")

    with patch("engine.apps.web.api.revoke_slack_token", new=revoke), TestClient(app) as client:
        response = client.post(
            "/api/slack/credentials",
            json={"clientId": "new-client", "clientSecret": "new-secret"},
        )

    assert response.status_code == 204
    assert events == ["revoke", "disconnect", "save"]
    revoke.assert_awaited_once_with("xoxb-old-token")


def test_changing_credentials_keeps_existing_state_when_revocation_fails(tmp_path) -> None:
    from engine.adapters.state_store.sqlite import SQLiteStateStore
    from engine.apps.web.api import create_app
    from engine.runtime import AgentSession, Capabilities

    stub = object()
    capabilities = Capabilities(stub, stub, stub, stub, stub, SQLiteStateStore(str(tmp_path / "s.sqlite3")))
    runners = {"default": stub}
    store = MagicMock(spec=SlackCredentialStore)
    store.token.return_value = "xoxb-old-token"
    app = create_app(AgentSession(capabilities, profiles={}, runners=runners), runners,
                     workflow_runners=runners, review_runners=runners,
                     workflow_catalog=MagicMock(), slack_credential_store=store)
    revoke = AsyncMock(side_effect=SlackAuthError("Slack unavailable"))

    with patch("engine.apps.web.api.revoke_slack_token", new=revoke), TestClient(app) as client:
        response = client.post(
            "/api/slack/credentials",
            json={"clientId": "new-client", "clientSecret": "new-secret"},
        )

    assert response.status_code == 502
    store.disconnect.assert_not_called()
    store.set_credentials.assert_not_called()


@pytest.mark.parametrize("operation", ["disconnect", "credentials"])
def test_successful_slack_mutation_invalidates_pending_oauth_flow(tmp_path, operation: str) -> None:
    from engine.adapters.state_store.sqlite import SQLiteStateStore
    from engine.apps.web.api import create_app
    from engine.runtime import AgentSession, Capabilities

    stub = object()
    capabilities = Capabilities(stub, stub, stub, stub, stub, SQLiteStateStore(str(tmp_path / "s.sqlite3")))
    runners = {"default": stub}
    store = MagicMock(spec=SlackCredentialStore)
    store.credentials.return_value = SlackCredentials("client", "secret")
    store.token.return_value = None
    app = create_app(AgentSession(capabilities, profiles={}, runners=runners), runners,
                     workflow_runners=runners, review_runners=runners,
                     workflow_catalog=MagicMock(), slack_credential_store=store)

    with patch("engine.apps.web.api.uuid4", return_value=MagicMock(hex="pending")), TestClient(app) as client:
        assert client.post("/api/slack/connect").status_code == 200
        if operation == "disconnect":
            response = client.post("/api/slack/disconnect")
        else:
            response = client.post(
                "/api/slack/credentials",
                json={"clientId": "new-client", "clientSecret": "new-secret"},
            )
        callback = client.get("/api/slack/callback?code=code&state=pending")

    assert response.status_code == 204
    assert callback.status_code == 400
    store.set_token.assert_not_called()
