from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient

from engine.apps.web.slack_auth import (
    SlackCredentialStore,
    SlackCredentials,
    authorization_url,
)


def test_authorization_url_requests_notification_scope_and_state() -> None:
    url = authorization_url("123", "http://localhost/api/slack/callback", "nonce")
    assert url.startswith("https://slack.com/oauth/v2/authorize?")
    assert "scope=chat%3Awrite" in url
    assert "state=nonce" in url


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
