"""Starting a work order by pinging the bot, and reporting it back.

Four things have to hold for the feature to be what it says it is: a mention
becomes a run, the run remembers where it came from, the agent can say
something mid-step, and the endings -- complete, fail, clarify, review ready --
arrive in the same thread.
"""

import asyncio
import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from engine.adapters.communications.slack import (
    SlackCredentials,
    SlackCredentialStore,
    mention_from_event,
    verify_signature,
)
from engine.adapters.state_store.memory import InMemoryStateStore
from engine.domain import (
    AgentId,
    AgentRunId,
    RunId,
    RunOrigin,
    RunState,
    StepId,
    StepSpec,
    TaskId,
    WorkflowId,
)
from engine.domain.chat import Message
from engine.ports import AgentTurn, Message as CommunicationsMessage
from engine.runtime import RunNotifier, WorkOrdersConfig
from engine.runtime.terminal_mcp import TerminalMcpBroker, TerminalResultRegistry
from permission_fakes import UNCLASSIFIED_PERMISSION_TRANSLATOR


SIGNING_SECRET = "shhh"


def _signed(body: bytes, secret: str = SIGNING_SECRET) -> dict[str, str]:
    timestamp = str(int(time.time()))
    signature = "v0=" + hmac.new(
        secret.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
    ).hexdigest()
    return {
        "x-slack-request-timestamp": timestamp,
        "x-slack-signature": signature,
        "content-type": "application/json",
    }


# --- reading a delivery ------------------------------------------------------


def test_mention_becomes_a_request_without_the_bot_token() -> None:
    mention = mention_from_event(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C123",
                "user": "U777",
                "ts": "1700.0001",
                "text": "<@UBOT|openengine> please add a   health endpoint",
            },
        }
    )
    assert mention is not None
    assert mention.text == "please add a health endpoint"
    assert (mention.channel, mention.author) == ("C123", "U777")
    # No thread yet, so the mention itself is the thread to answer under.
    assert mention.thread_id == "1700.0001"


def test_mention_inside_a_thread_answers_that_thread() -> None:
    mention = mention_from_event(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C123",
                "user": "U777",
                "ts": "1700.0009",
                "thread_ts": "1700.0001",
                "text": "<@UBOT> do it",
            },
        }
    )
    assert mention is not None and mention.thread_id == "1700.0001"


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "url_verification", "challenge": "abc"},
        {"type": "event_callback", "event": {"type": "message", "text": "hi"}},
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "user": "U1",
                "ts": "1",
                "bot_id": "B1",
                "text": "<@UBOT> loop",
            },
        },
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "user": "U1",
                "ts": "1",
                "text": "<@UBOT>",
            },
        },
    ],
    ids=["handshake", "other-event", "the-bot-itself", "nothing-asked"],
)
def test_deliveries_that_are_not_a_request(payload: dict) -> None:
    assert mention_from_event(payload) is None


def test_signature_accepts_slack_and_refuses_everything_else() -> None:
    body = b'{"type":"event_callback"}'
    headers = _signed(body)
    assert verify_signature(
        SIGNING_SECRET,
        headers["x-slack-request-timestamp"],
        headers["x-slack-signature"],
        body,
    )
    assert not verify_signature(
        "another-secret",
        headers["x-slack-request-timestamp"],
        headers["x-slack-signature"],
        body,
    )
    assert not verify_signature(
        SIGNING_SECRET,
        headers["x-slack-request-timestamp"],
        headers["x-slack-signature"],
        body + b" ",
    )
    # A capture replayed an hour later is refused even though it verifies.
    stale = str(int(time.time()) - 3600)
    replayed = "v0=" + hmac.new(
        SIGNING_SECRET.encode(),
        b"v0:" + stale.encode() + b":" + body,
        hashlib.sha256,
    ).hexdigest()
    assert not verify_signature(SIGNING_SECRET, stale, replayed, body)


@pytest.mark.parametrize("timestamp", ["nan", "inf", "-inf", "not-a-number"])
def test_a_timestamp_that_is_not_a_time_is_refused(timestamp: str) -> None:
    """The age check has to reject these, not fall through to the digest.

    `float("nan")` parses, and every comparison against NaN is False -- so an
    age test written the obvious way round waves it past the only guard there
    is against a replay.
    """
    body = b'{"type":"event_callback"}'
    signature = "v0=" + hmac.new(
        SIGNING_SECRET.encode(),
        b"v0:" + timestamp.encode() + b":" + body,
        hashlib.sha256,
    ).hexdigest()
    assert not verify_signature(SIGNING_SECRET, timestamp, signature, body)


# --- the endpoint ------------------------------------------------------------


class RecordingCommunications:
    """A chat provider that remembers what was said and where."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, CommunicationsMessage | str, str]] = []

    async def post(self, channel, message, run_id=None, thread_id="") -> str:
        self.posts.append((channel, message, thread_id))
        return "1700.0002"

    async def reply(self, message_id: str, message: str) -> str:  # pragma: no cover
        raise NotImplementedError


class _FakeMcpRunner:
    """A minimal runner that satisfies ``McpAgentRunner`` for tests.

    Returns a canned greeting from the concierge without calling any tools.
    """

    permission_translator = UNCLASSIFIED_PERMISSION_TRANSLATOR

    async def run_turn(self, agent_run_id, profile, messages, tools=(), workspace_id=None):
        return AgentTurn(message=Message.assistant("Hi, how can I help?"))

    async def run_turn_with_mcp(self, agent_run_id, profile, messages, mcp_server, workspace_id=None):
        return AgentTurn(message=Message.assistant("Hi, how can I help?"))

    async def cancel(self, agent_run_id):
        pass


def _app(tmp_path, communications, work_orders: WorkOrdersConfig, catalog=None, provider=None, github_login_config=None, graph_runtime=None, github_comment_handler=None, github_webhook_secret="", github_bot_login="", approval_policy=None):
    from engine.apps.web.api import create_app
    from engine.runtime import AgentSession, Capabilities, WorkflowCatalog

    stub = object()
    runner = _FakeMcpRunner()
    capabilities = Capabilities(
        workflow_runtime=stub,
        source_control=stub,
        agent_runner=runner,
        communications=communications,
        workspace_provider=stub,
        state_store=InMemoryStateStore(),
    )
    runners = {"default": runner}
    session = AgentSession(capabilities, profiles={}, runners=runners)
    slack_store = MagicMock(spec=SlackCredentialStore)
    slack_store.credentials.return_value = SlackCredentials("client", "secret")
    slack_store.token.return_value = "xoxb-token"
    slack_store.signing_secret.return_value = SIGNING_SECRET
    return create_app(
        session,
        runners,
        workflow_catalog=(
            catalog if catalog is not None else WorkflowCatalog.from_graphs(())
        ),
        **({} if approval_policy is None else {"approval_policy": approval_policy}),
        slack_credential_store=slack_store,
        github_login_config=github_login_config,
        public_url="https://engine.example",
        work_orders=work_orders,
        credential_store=MagicMock(),
        concierge_provider=provider or FakeACPProvider(),
        graph_runtime=graph_runtime,
        github_comment_handler=github_comment_handler,
        github_webhook_secret=lambda: github_webhook_secret,
        github_repository="acme/api",
        github_bot_login=github_bot_login,
    ), capabilities, slack_store

def _mention_graph():
    """The workflow these mentions name, doing nothing in particular."""
    from engine.graph_runtime_langgraph import State, graph_workflow
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(State)
    builder.add_node("work", lambda state: {})
    builder.add_edge(START, "work")
    builder.add_edge("work", END)
    return graph_workflow(
        builder, id="implementation-review-v1", name="Implementation review"
    )

def _workflow_catalog():
    """A catalog holding the workflow these mentions name."""
    from engine.runtime import WorkflowCatalog

    return WorkflowCatalog.from_graphs((_mention_graph(),))


def test_handshake_is_answered_with_the_challenge(tmp_path) -> None:
    from starlette.testclient import TestClient

    app, _capabilities, slack_store = _app(tmp_path, RecordingCommunications(), WorkOrdersConfig())
    body = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
    with TestClient(app) as client:
        response = client.post("/api/slack/events", content=body, headers=_signed(body))
        client.portal.call(app.state.slack_ingress.drain)
    assert response.status_code == 200
    assert response.json() == {"challenge": "abc"}


def test_an_unsigned_delivery_starts_nothing(tmp_path) -> None:
    from starlette.testclient import TestClient

    communications = RecordingCommunications()
    app, capabilities, slack_store = _app(
        tmp_path,
        communications,
        WorkOrdersConfig(repository="acme/api", workflow="implementation-review-v1"),
        _workflow_catalog(),
    )
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C1",
                "user": "U1",
                "ts": "1",
                "text": "<@UBOT> do something",
            },
        }
    ).encode()
    with TestClient(app) as client:
        response = client.post(
            "/api/slack/events",
            content=body,
            headers={
                "x-slack-request-timestamp": str(int(time.time())),
                "x-slack-signature": "v0=not-a-signature",
            },
        )
    assert response.status_code == 401
    assert communications.posts == []
    assert asyncio.run(capabilities.state_store.list_runs()) == ()


def test_a_mention_replies_through_the_concierge(tmp_path) -> None:
    """A mention routes through the concierge agent and replies in thread."""
    from starlette.testclient import TestClient

    communications = RecordingCommunications()
    app, capabilities, slack_store = _app(
        tmp_path,
        communications,
        WorkOrdersConfig(
            repository="acme/api",
            workflow="implementation-review-v1",
            runner="default",
        ),
        _workflow_catalog(),
    )
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C123",
                "user": "U777",
                "ts": "1700.0001",
                "text": "<@UBOT> add a health endpoint",
            },
        }
    ).encode()
    with TestClient(app) as client:
        response = client.post("/api/slack/events", content=body, headers=_signed(body))
        client.portal.call(app.state.slack_ingress.drain)

    assert response.status_code == 200
    # The concierge replies in the thread with its greeting.
    channel, message, thread_id = communications.posts[0]
    assert (channel, thread_id) == ("C123", "1700.0001")
    assert message.mention == "U777"
    assert "Hi, how can I help?" in message.text


def test_a_redelivery_is_ignored(tmp_path) -> None:
    from starlette.testclient import TestClient

    communications = RecordingCommunications()
    app, capabilities, slack_store = _app(
        tmp_path,
        communications,
        WorkOrdersConfig(
            repository="acme/api",
            workflow="implementation-review-v1",
            runner="default",
        ),
        _workflow_catalog(),
    )
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C123",
                "user": "U777",
                "ts": "1700.0001",
                "text": "<@UBOT> add a health endpoint",
            },
        }
    ).encode()
    with TestClient(app) as client:
        client.post("/api/slack/events", content=body, headers=_signed(body))
        retry = client.post(
            "/api/slack/events",
            content=body,
            headers={**_signed(body), "x-slack-retry-num": "1"},
        )

        client.portal.call(app.state.slack_ingress.drain)

    assert retry.status_code == 200
    # Only one reply — the redelivery was ignored.
    assert len(communications.posts) == 1


def test_a_mention_without_config_still_greets(tmp_path) -> None:
    """Even without work_orders config, the concierge greets the user."""
    from starlette.testclient import TestClient

    communications = RecordingCommunications()
    app, capabilities, slack_store = _app(
        tmp_path, communications, WorkOrdersConfig(), _workflow_catalog()
    )
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C123",
                "user": "U777",
                "ts": "1700.0001",
                "text": "<@UBOT> add a health endpoint",
            },
        }
    ).encode()
    with TestClient(app) as client:
        response = client.post("/api/slack/events", content=body, headers=_signed(body))
        client.portal.call(app.state.slack_ingress.drain)

    assert response.status_code == 200
    # The concierge greets regardless of work_orders config — it is the
    # create_workorder tool that checks repositories, not the greeting.
    _channel, message, thread_id = communications.posts[0]
    assert thread_id == "1700.0001"
    assert "Hi, how can I help?" in message.text


def test_a_mention_starts_nothing_while_slack_is_disconnected(tmp_path) -> None:
    """An app stays installed after this server disconnects, so mentions arrive.

    Starting one would provision a workspace and run a write-access agent to
    completion with every reply -- including a refusal -- dropped on the floor.
    """
    from starlette.testclient import TestClient

    communications = RecordingCommunications()
    app, capabilities, slack_store = _app(
        tmp_path,
        communications,
        WorkOrdersConfig(
            repository="acme/api",
            workflow="implementation-review-v1",
            runner="default",
        ),
        _workflow_catalog(),
    )
    slack_store.token.return_value = None
    body = json.dumps(
        {
            "type": "event_callback",
            "event": {
                "type": "app_mention",
                "channel": "C123",
                "user": "U777",
                "ts": "1700.0001",
                "text": "<@UBOT> add a health endpoint",
            },
        }
    ).encode()
    with TestClient(app) as client:
        response = client.post("/api/slack/events", content=body, headers=_signed(body))
        client.portal.call(app.state.slack_ingress.drain)
        # And the panel does not claim otherwise while it is in that state.
        status = client.get("/api/slack/status").json()

    assert response.status_code == 200
    assert asyncio.run(capabilities.state_store.list_runs()) == ()
    assert communications.posts == []
    assert status["connected"] is False
    assert status["events"] is False


# --- reporting back ----------------------------------------------------------


def test_update_status_is_served_only_to_a_run_with_somewhere_to_report() -> None:
    async def scenario() -> None:
        reported: list[str] = []

        async def report(status: str) -> None:
            reported.append(status)

        step = StepSpec(StepId("implementation"), AgentId("coder"))
        silent = TerminalMcpBroker(
            run_id=RunId("run-1"),
            agent_run_id=AgentRunId("agent-run-1"),
            step=step,
            registry=TerminalResultRegistry(),
        )
        async with silent:
            assert "--status-updates" not in silent.config.args
            refused = await silent._submit(
                {
                    "token": silent._token,
                    "request_id": 1,
                    "name": "update_status",
                    "arguments": {"status": "working on it"},
                }
            )
        assert refused["ok"] is False

        broker = TerminalMcpBroker(
            run_id=RunId("run-1"),
            agent_run_id=AgentRunId("agent-run-2"),
            step=step,
            registry=TerminalResultRegistry(),
        )
        broker.enable_status_updates(report)
        async with broker:
            assert "--status-updates" in broker.config.args
            accepted = await broker._submit(
                {
                    "token": broker._token,
                    "request_id": 2,
                    "name": "update_status",
                    "arguments": {"status": "reading the code"},
                }
            )
            blank = await broker._submit(
                {
                    "token": broker._token,
                    "request_id": 3,
                    "name": "update_status",
                    "arguments": {"status": "  "},
                }
            )
        assert accepted["ok"] is True
        assert blank["ok"] is False
        assert reported == ["reading the code"]

    asyncio.run(scenario())


def test_clarify_is_reported_because_no_event_carries_it() -> None:
    async def scenario() -> None:
        reported: list[str] = []

        broker = TerminalMcpBroker(
            run_id=RunId("run-1"),
            agent_run_id=AgentRunId("agent-run-1"),
            step=StepSpec(StepId("implementation"), AgentId("coder")),
            registry=TerminalResultRegistry(),
        )
        broker.enable_status_updates(lambda status: _record(reported, status))
        async with broker:
            response = await broker._submit(
                {
                    "token": broker._token,
                    "request_id": 1,
                    "name": "clarify",
                    "arguments": {},
                }
            )
        assert response["acknowledgement"] == "clarified"
        assert reported == ["answered a question without changing the work order"]

    asyncio.run(scenario())


async def _record(sink: list[str], status: str) -> None:
    sink.append(status)


def test_a_run_from_the_web_is_never_announced() -> None:
    communications = RecordingCommunications()
    notifier = RunNotifier(communications, "https://engine.example")
    state = RunState(
        run_id=RunId("run-1"),
        task_id=TaskId("task-1"),
        workflow_id=WorkflowId("implementation-review-v1"),
    )
    asyncio.run(notifier.announce(state, "half way there"))
    assert communications.posts == []


def test_the_signing_secret_can_be_added_without_reconnecting(tmp_path) -> None:
    """Enabling mentions must not cost an operator their Slack connection.

    Saving the OAuth pair revokes the token and starts the flow over, which is
    right when the app changes and wrong as the price of one extra secret.
    """
    from starlette.testclient import TestClient

    app, _capabilities, slack_store = _app(tmp_path, RecordingCommunications(), WorkOrdersConfig())
    store = slack_store
    store.signing_secret.return_value = None

    with (
        patch(
            "engine.apps.web.api.revoke_slack_token", new=AsyncMock()
        ) as revoke,
        TestClient(app) as client,
    ):
        response = client.post(
            "/api/slack/credentials", json={"signingSecret": "shhh"}
        )

    assert response.status_code == 204
    store.set_signing_secret.assert_called_once_with("shhh")
    store.set_credentials.assert_not_called()
    store.disconnect.assert_not_called()
    revoke.assert_not_awaited()


def test_the_signing_secret_alone_needs_credentials_already_saved(tmp_path) -> None:
    from starlette.testclient import TestClient

    app, _capabilities, slack_store = _app(tmp_path, RecordingCommunications(), WorkOrdersConfig())
    store = slack_store
    store.credentials.return_value = None

    with TestClient(app) as client:
        response = client.post(
            "/api/slack/credentials", json={"signingSecret": "shhh"}
        )

    assert response.status_code == 409
    store.set_signing_secret.assert_not_called()


class BrokenCommunications:
    async def post(self, *_args, **_kwargs) -> str:
        raise RuntimeError("Slack is unavailable")

    async def reply(self, *_args) -> str:  # pragma: no cover
        raise NotImplementedError


def _origin_state() -> RunState:
    return RunState(
        run_id=RunId("run-1"),
        task_id=TaskId("task-1"),
        workflow_id=WorkflowId("implementation-review-v1"),
        origin=RunOrigin(channel="C1", thread_id="1700.0001", author="U1"),
    )


def test_a_provider_that_is_down_does_not_break_the_run() -> None:
    notifier = RunNotifier(BrokenCommunications(), "https://engine.example")
    asyncio.run(notifier.announce(_origin_state(), "half way there"))


def test_an_agent_is_told_when_its_status_did_not_reach_anyone() -> None:
    """The acknowledgement has to be true, or it is worse than no answer.

    An agent told "status posted" by a step whose status went nowhere will not
    mention the gap or say it again, so the one path with somebody waiting on
    the answer reports the failure instead of swallowing it.
    """

    async def scenario() -> None:
        notifier = RunNotifier(BrokenCommunications(), "https://engine.example")
        state = _origin_state()

        async def report(status: str) -> None:
            await notifier.deliver(state, f"*Implementation*: {status}")

        broker = TerminalMcpBroker(
            run_id=state.run_id,
            agent_run_id=AgentRunId("agent-run-1"),
            step=StepSpec(StepId("implementation"), AgentId("coder")),
            registry=TerminalResultRegistry(),
        )
        broker.enable_status_updates(report)
        async with broker:
            answer = await broker._submit(
                {
                    "token": broker._token,
                    "request_id": 1,
                    "name": "update_status",
                    "arguments": {"status": "reading the code"},
                }
            )
            # The step is not ended by it: the run is the thing that matters,
            # and a `clarify` in the same session still answers normally.
            clarified = await broker._submit(
                {
                    "token": broker._token,
                    "request_id": 2,
                    "name": "clarify",
                    "arguments": {},
                }
            )
        assert answer["ok"] is False
        assert "Slack is unavailable" in answer["error"]
        assert clarified == {"ok": True, "acknowledgement": "clarified"}

    asyncio.run(scenario())


# --- concierge broker ---------------------------------------------------------


def test_concierge_broker_creates_a_work_order() -> None:
    """The create_workorder tool calls the factory callback and returns the URL."""
    from engine.slack_concierge.slack_egress import ConciergeBroker

    async def scenario() -> None:
        created: list[tuple[str, str]] = []

        async def create(repository: str, prompt: str) -> tuple[str, str]:
            created.append((repository, prompt))
            return "https://engine.example/runs/run-abc", "run-abc"

        broker = ConciergeBroker(
            create_workorder=create,
            default_repository="acme/api",
        )
        async with broker:
            result = await broker._submit(
                {
                    "token": broker._token,
                    "name": "create_workorder",
                    "arguments": {"prompt": "add a health endpoint"},
                }
            )
        assert result["ok"] is True
        assert "run-abc" in result["text"]
        assert created == [("acme/api", "add a health endpoint")]

    asyncio.run(scenario())


def test_concierge_broker_falls_back_to_dot_when_no_default() -> None:
    """Without a configured default the broker uses '.' (current directory)."""
    from engine.slack_concierge.slack_egress import ConciergeBroker

    async def scenario() -> None:
        created: list[tuple[str, str]] = []

        async def create(repository: str, prompt: str) -> tuple[str, str]:
            created.append((repository, prompt))
            return "https://engine.example/runs/run-1", "run-1"

        broker = ConciergeBroker(create_workorder=create, default_repository="")
        async with broker:
            result = await broker._submit(
                {
                    "token": broker._token,
                    "name": "create_workorder",
                    "arguments": {"prompt": "do something"},
                }
            )
        assert result["ok"] is True
        assert created == [(".", "do something")]

    asyncio.run(scenario())


def test_concierge_broker_uses_configured_default_repository() -> None:
    """The configured default repository is always used."""
    from engine.slack_concierge.slack_egress import ConciergeBroker

    async def scenario() -> None:
        created: list[tuple[str, str]] = []

        async def create(repository: str, prompt: str) -> tuple[str, str]:
            created.append((repository, prompt))
            return "https://engine.example/runs/run-1", "run-1"

        broker = ConciergeBroker(
            create_workorder=create,
            default_repository="acme/api",
        )
        async with broker:
            result = await broker._submit(
                {
                    "token": broker._token,
                    "name": "create_workorder",
                    "arguments": {
                        "prompt": "fix the bug",
                    },
                }
            )
        assert result["ok"] is True
        assert created == [("acme/api", "fix the bug")]

    asyncio.run(scenario())


def test_concierge_broker_rejects_unknown_arguments() -> None:
    """Extra arguments (like repository) are rejected."""
    from engine.slack_concierge.slack_egress import ConciergeBroker

    async def scenario() -> None:
        async def create(repository: str, prompt: str) -> tuple[str, str]:
            raise AssertionError("should not be called")

        broker = ConciergeBroker(
            create_workorder=create,
            default_repository="",
        )
        async with broker:
            result = await broker._submit(
                {
                    "token": broker._token,
                    "name": "create_workorder",
                    "arguments": {
                        "prompt": "fix the bug",
                        "repository": "acme/frontend",
                    },
                }
            )
        assert result["ok"] is False
        assert "unknown" in result["error"]

    asyncio.run(scenario())


def test_concierge_broker_rejects_empty_prompt() -> None:
    from engine.slack_concierge.slack_egress import ConciergeBroker

    async def scenario() -> None:
        async def create(repository: str, prompt: str) -> tuple[str, str]:
            raise AssertionError("should not be called")

        broker = ConciergeBroker(
            create_workorder=create, default_repository="acme/api"
        )
        async with broker:
            result = await broker._submit(
                {
                    "token": broker._token,
                    "name": "create_workorder",
                    "arguments": {"prompt": "  "},
                }
            )
        assert result["ok"] is False
        assert "prompt" in result["error"]

    asyncio.run(scenario())


def test_concierge_broker_rejects_unknown_tool() -> None:
    from engine.slack_concierge.slack_egress import ConciergeBroker

    async def scenario() -> None:
        async def create(repository: str, prompt: str) -> tuple[str, str]:
            raise AssertionError("should not be called")

        broker = ConciergeBroker(
            create_workorder=create, default_repository="acme/api"
        )
        async with broker:
            result = await broker._submit(
                {
                    "token": broker._token,
                    "name": "complete_step",
                    "arguments": {},
                }
            )
        assert result["ok"] is False
        assert "unknown" in result["error"]

    asyncio.run(scenario())


# --- concierge MCP protocol --------------------------------------------------
#
# What a Slack agent's CLI sees when it connects. The transport answering these
# is shared and tested once in `test_single_tool_mcp.py`; kept here as well
# because the answers are this surface's, and a shared implementation is
# exactly where a change made for the other surface could quietly alter them.


def _slack_mcp_answer(request: object) -> dict[str, object] | None:
    from engine.single_tool_mcp import mcp_response
    from engine.slack_concierge import slack_egress

    async def scenario() -> dict[str, object] | None:
        # Port 0 connects to nothing: none of these reach the host, which is
        # part of what they assert.
        return await mcp_response(
            "127.0.0.1", 0, "tok", request,
            tool_spec=slack_egress._TOOL_SPEC,
            server_info_name=slack_egress._SERVER_INFO_NAME,
        )

    return asyncio.run(scenario())


def test_mcp_initialize_returns_server_protocol_version() -> None:
    """The server always returns its own version, not the client's."""
    from engine.single_tool_mcp import PROTOCOL_VERSION

    result = _slack_mcp_answer(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "1999-01-01",
            "clientInfo": {"name": "test", "version": "1"},
        }},
    )
    assert result is not None
    assert result["result"]["protocolVersion"] == PROTOCOL_VERSION


def test_mcp_tools_list_returns_create_workorder() -> None:
    result = _slack_mcp_answer({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert result is not None
    tools = result["result"]["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == "create_workorder"


def test_mcp_notifications_are_swallowed() -> None:
    assert _slack_mcp_answer(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    ) is None


def test_mcp_unknown_method_returns_error() -> None:
    result = _slack_mcp_answer(
        {"jsonrpc": "2.0", "id": 3, "method": "resources/list"}
    )
    assert result is not None
    assert result["error"]["code"] == -32601



class FakeACPProvider:
    name = "fake"

    def __init__(self, text="Hi, how can I help?", fail=False, create=False,
                 fail_after_create=False, calls=1):
        self.text, self.fail, self.create = text, fail, create
        self.clients = []
        self.fail_after_create = fail_after_create
        #: How many times the model calls the tool in one turn. More than one
        #: is a model that split a request, or was talked into asking twice.
        self.calls = calls

    async def connect(self):
        provider = self
        class Client:
            closed = False
            prompts = []
            async def new_session(self, *, cwd, mcp_servers):
                self.config = mcp_servers[0]
                self.prompts = []
                return self
            async def prompt(self, prompt):
                from langgraph_acp.events import ACPEvent, ACPEventType
                self.prompts.append(prompt)
                if provider.fail:
                    provider.fail = False
                    raise RuntimeError("transient")
                if provider.create and "new workorder" in prompt:
                    self.results = await call_mcp(self.config, calls=provider.calls)
                    self.result = self.results[-1]
                    if provider.fail_after_create:
                        raise RuntimeError("failed after accepting work")
                yield ACPEvent(agent="fake", type=ACPEventType.MESSAGE_DELTA,
                               data={"content": {"type": "text", "text": provider.text}})
            async def close(self):
                self.closed = True
        client = Client()
        self.clients.append(client)
        return client


async def call_mcp(config, calls=1):
    """Real stdio child -> TCP broker -> injected host callback.

    The tool is whichever one the broker advertises, so the same fake drives
    the Slack broker and the pull-request one without knowing either.

    ``calls`` is how many times the tool is called down the one session, which
    is what a model doing so within a single turn looks like from here.
    """
    process = await asyncio.create_subprocess_exec(
        config["command"], *config["args"], stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )

    async def send(request):
        process.stdin.write(json.dumps(request).encode() + b"\n")
        await process.stdin.drain()

    async def roundtrip(request):
        await send(request)
        return json.loads(await process.stdout.readline())

    try:
        initialized = await roundtrip(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "unsupported"}})
        assert initialized["result"]["protocolVersion"] == "2025-06-18"
        await send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        listed = await roundtrip({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = listed["result"]["tools"]
        assert len(tools) == 1, tools
        answers = [
            await roundtrip(
                {"jsonrpc": "2.0", "id": 3 + call, "method": "tools/call",
                 "params": {"name": tools[0]["name"],
                            "arguments": {"prompt": "Implement it"}}})
            for call in range(calls)
        ]
    finally:
        process.stdin.close()
        _stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()
    return [answer["result"] for answer in answers]


def test_concierge_graph_reuse_eviction_failure_and_empty_reply():
    from engine.slack_concierge import IncomingMessage, SlackConcierge

    async def scenario():
        provider = FakeACPProvider(text=" ")
        replies = []
        async def reply(origin, text):
            replies.append(text)
        async def create(origin, repository, prompt):
            return "url", "id"
        agent = SlackConcierge(provider=provider, reply=reply, create_workorder=create, max_threads=1)
        def message(thread):
            return IncomingMessage(RunOrigin(channel="C", thread_id=thread, author="U"), "hello")
        await agent.handle(message("1"))
        await agent.handle(message("1"))
        assert len(provider.clients) == 1
        assert len(provider.clients[0].prompts) == 2
        assert replies == ["I'm working on that."] * 2
        await agent.handle(message("2"))
        assert provider.clients[0].closed
        assert not agent.has_thread("C", "1")
        provider.fail = True
        import pytest
        with pytest.raises(RuntimeError, match="transient"):
            await agent.handle(message("2"))
        assert not agent.has_thread("C", "2")
        await agent.handle(message("2"))
        assert len(provider.clients) == 3
        await agent.close()
        assert all(c.closed for c in provider.clients)
    asyncio.run(scenario())


@pytest.mark.parametrize("fail_after_create", [False, True])
def test_thread_reply_creates_workorder_through_stdio_mcp(tmp_path, fail_after_create):
    from starlette.testclient import TestClient
    from engine.graph_runtime_langgraph.workflows import sqlite_runtime

    graph = _mention_graph()
    provider = FakeACPProvider(create=True, fail_after_create=fail_after_create)
    communications = RecordingCommunications()
    app, capabilities, _ = _app(tmp_path, communications,
        WorkOrdersConfig(repository="acme/api", workflow="implementation-review-v1", runner="default"),
        _workflow_catalog(), provider=provider,
        graph_runtime=sqlite_runtime((graph,), tmp_path / "graph"))
    def body(kind, ts, text, **extra):
        return json.dumps({"type": "event_callback", "event": dict(
            type=kind, channel="C", user="U", ts=ts, text=text, **extra)}).encode()
    with TestClient(app) as client:
        greeting = body("app_mention", "1", "<@BOT>")
        assert client.post("/api/slack/events", content=greeting, headers=_signed(greeting)).status_code == 200
        client.portal.call(app.state.slack_ingress.drain)
        assert not client.portal.call(capabilities.state_store.list_runs)
        request = body("message", "2", "new workorder please", thread_ts="1")
        client.post("/api/slack/events", content=request, headers=_signed(request))
        client.portal.call(app.state.slack_ingress.drain)
        # Both Slack event kinds describe the same message; execute only once.
        duplicate = body("app_mention", "2", "new workorder please", thread_ts="1")
        client.post("/api/slack/events", content=duplicate, headers=_signed(duplicate))
        client.portal.call(app.state.slack_ingress.drain)
        runs = client.portal.call(capabilities.state_store.list_runs)
        assert len(runs) == 1
        assert runs[0].origin.thread_id == "1"
        result = provider.clients[0].result
        assert not result.get("isError"), result
        assert result["structuredContent"]["url"].startswith("https://engine.example")
        assert len(provider.clients[0].prompts) == 2
    announcements = [
        m for _, m, _ in communications.posts
        if m.text.startswith("Started a work order")
    ]
    assert len(announcements) == 1
    assert announcements[0].links
    assert any(m.links for _, m, _ in communications.posts)
    assert all(thread == "1" for _, _, thread in communications.posts)
    # The work-order announcement (with the link) must follow the conversational
    # reply so that messages appear in the expected order in the thread.
    texts = [m.text for _, m, _ in communications.posts]
    if not fail_after_create:
        assert texts.index(provider.text) < texts.index(announcements[0].text), (
            "announcement with link should appear after the conversational reply"
        )


def test_concierge_uses_real_langgraph_acp_session(tmp_path):
    from pathlib import Path
    import sys
    from langgraph_acp.agent import StdioACPProvider
    from engine.slack_concierge import IncomingMessage, SlackConcierge

    async def scenario():
        log = tmp_path / "acp.jsonl"
        provider = StdioACPProvider(name="fake", command=[sys.executable,
            str(Path(__file__).resolve().parents[1] / "langgraph-acp/tests/fake_agent.py")],
            env={"FAKE_AGENT_LOG": str(log)})
        replies = []
        async def reply(origin, text):
            replies.append(text)
        async def create(origin, repository, prompt):
            raise AssertionError("greetings must not start work")
        agent = SlackConcierge(provider=provider, reply=reply, create_workorder=create)
        message = IncomingMessage(
            RunOrigin(channel="C", thread_id="1", author="U"), "hello")
        try:
            await agent.handle(message)
            await agent.handle(message)
        finally:
            await agent.close()
        requests = [json.loads(line) for line in log.read_text().splitlines()]
        new = [r for r in requests if r.get("method") == "session/new"]
        assert len(new) == 1
        config = new[0]["params"]["mcpServers"][0]
        assert config["name"] == "concierge"
        assert "--token" not in config["args"]
        assert not Path(config["args"][-1]).exists()
        assert len([r for r in requests if r.get("method") == "session/prompt"]) == 2
        assert len(replies) == 2
    asyncio.run(scenario())


def test_ingress_filters_messages_and_bounds_queue():
    from engine.slack_concierge import SlackIngress

    async def scenario():
        gate = asyncio.Event()
        messages = []
        class Concierge:
            def has_thread(self, channel, thread_id):
                return False
            async def handle(self, message):
                messages.append(message)
                await gate.wait()
            async def close(self):
                pass
        ingress = SlackIngress(Concierge(), capacity=1)
        def payload(kind, ts, **extra):
            return {"type": "event_callback", "event": dict(type=kind,
                channel="C", user="U", ts=ts, text="hello", **extra)}
        assert ingress.accept(payload("message", "0", thread_ts="unknown"))
        assert ingress.accept(payload("app_mention", "0", bot_id="bot"))
        assert ingress.accept(payload("message", "0", subtype="message_changed"))
        assert not messages
        assert ingress.accept(payload("app_mention", "1"))
        await asyncio.sleep(0)
        assert len(messages) == 1  # turn is blocked; accept already returned
        assert ingress.accept(payload("message", "2", thread_ts="1"))
        assert not ingress.accept(payload("app_mention", "3"))
        gate.set()
        await ingress.drain()
        assert ingress.accept(payload("app_mention", "3"))  # rejected event can retry
        await ingress.drain()
        assert [m.origin.thread_id for m in messages] == ["1", "1", "3"]
        await ingress.close()
    asyncio.run(scenario())


def test_ingress_reacts_with_eyes_before_handling():
    from engine.slack_concierge import SlackIngress

    async def scenario():
        order: list[str] = []
        messages = []

        class Concierge:
            def has_thread(self, channel, thread_id):
                return False
            async def handle(self, message):
                order.append("handle")
                messages.append(message)
            async def close(self):
                pass

        reacted: list[tuple[str, str, str]] = []

        async def react(channel, ts, emoji):
            order.append("react")
            reacted.append((channel, ts, emoji))

        ingress = SlackIngress(Concierge(), capacity=1, react=react)

        def payload(kind, ts, **extra):
            return {"type": "event_callback", "event": dict(
                type=kind, channel="C1", user="U1", ts=ts, text="hello", **extra)}

        ingress.accept(payload("app_mention", "1700.0001"))
        await ingress.drain()

        assert reacted == [("C1", "1700.0001", "eyes")]
        assert len(messages) == 1
        assert order == ["react", "handle"]

    asyncio.run(scenario())


def test_ingress_failing_react_does_not_prevent_handle():
    from engine.slack_concierge import SlackIngress

    async def scenario():
        messages = []

        class Concierge:
            def has_thread(self, channel, thread_id):
                return False
            async def handle(self, message):
                messages.append(message)
            async def close(self):
                pass

        async def failing_react(channel, ts, emoji):
            raise RuntimeError("Slack API down")

        ingress = SlackIngress(Concierge(), capacity=1, react=failing_react)

        def payload(kind, ts, **extra):
            return {"type": "event_callback", "event": dict(
                type=kind, channel="C1", user="U1", ts=ts, text="hello", **extra)}

        ingress.accept(payload("app_mention", "1700.0001"))
        await ingress.drain()

        # handle was still called despite the react failure
        assert len(messages) == 1

    asyncio.run(scenario())


def test_concierge_permissions_only_allow_the_granted_tool():
    from engine.slack_concierge.slack_egress import tool_permission
    from langgraph_acp.permissions import ACPPermissionRequest, ACPPermissionOption

    async def scenario():
        for name, allowed in [("mcp__concierge__create_workorder", True), ("Bash", False), ({}, False)]:
            result = await tool_permission(ACPPermissionRequest(agent="codex",
                tool_call={"name": name}, options=(ACPPermissionOption("yes", kind="allow_once"),)))
            assert result.granted == allowed
    asyncio.run(scenario())
@pytest.mark.parametrize("valid_signature", [True, False])
def test_slack_signature_auth_with_github_login_enabled(tmp_path, valid_signature):
    from starlette.testclient import TestClient
    from engine.apps.web.github_login import GitHubLoginConfig

    app, _, _ = _app(
        tmp_path, RecordingCommunications(), WorkOrdersConfig(),
        github_login_config=GitHubLoginConfig(
            "client", "secret", "https://engine.example/api/auth/github/callback"
        ),
    )
    body = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
    headers = _signed(body)
    if not valid_signature:
        headers["x-slack-signature"] = "v0=invalid"
    with TestClient(app) as client:
        assert client.get("/api/config").status_code == 401
        response = client.post("/api/slack/events", content=body, headers=headers)
    if valid_signature:
        assert response.status_code == 200
        assert response.json() == {"challenge": "abc"}
    else:
        assert response.status_code == 401


def test_checked_in_slack_repository_is_current_checkout():
    from pathlib import Path
    import tomllib

    config = tomllib.loads((Path(__file__).resolve().parents[1] / "engine.toml").read_text())
    assert config["work_orders"]["repository"] == "."


@pytest.mark.parametrize("ending", ("finished", "human_review", "failed"))
@pytest.mark.parametrize("before_row", (False, True))
def test_slack_starts_configured_graph_with_input_defaults(tmp_path, ending, before_row):
    from starlette.testclient import TestClient
    from engine.graph_runtime_langgraph import State, WorkflowInput, graph_workflow
    from engine.graph_runtime_langgraph.workflows import sqlite_runtime
    from engine.runtime import WorkflowCatalog
    from engine.runtime.config import load_engine_config
    from langgraph.graph import START, END, StateGraph
    from pathlib import Path

    configured = load_engine_config(Path(__file__).resolve().parents[1] / "engine.toml")
    assert configured.config.work_orders.workflow == "implementation-review-rerank"
    builder = StateGraph(State)
    builder.add_node("work", lambda state: {"received": state["inputs"]})
    builder.add_edge(START, "work")
    if ending == "human_review":
        from engine.graph_runtime_langgraph.components import HumanReviewNode
        builder.add_node("decision", HumanReviewNode())
        builder.add_edge("work", "decision")
        builder.add_edge("decision", END)
    elif ending == "failed":
        def fail(state):
            raise RuntimeError("review service unavailable")
        builder.add_node("failure", fail)
        builder.add_edge("work", "failure")
        builder.add_edge("failure", END)
    else:
        builder.add_edge("work", END)
    graph = graph_workflow(
        builder, id="implementation-review-rerank", name="Implementation review rerank",
        inputs=(WorkflowInput("implementation_runner", "Implementation runner", "codex"),
                WorkflowInput("review_runner", "Review runner", "claude")),
    )
    # Force the graph to reach its ending before start() returns: notifications
    # must survive events arriving before the WorkOrder row/origin is saved.
    from contextlib import asynccontextmanager
    from engine.graph_runtime import RunStatus

    @asynccontextmanager
    async def runtime_before_row():
        async with sqlite_runtime((graph,), tmp_path / "graph") as runtime:
            start = runtime.start
            async def start_and_wait(*args, **kwargs):
                run = await start(*args, **kwargs)
                async with asyncio.timeout(10):
                    while (await runtime.snapshot(run.run_id)).status is RunStatus.RUNNING:
                        await asyncio.sleep(0.01)
                return await runtime.snapshot(run.run_id)
            if before_row:
                runtime.start = start_and_wait
            yield runtime

    provider = FakeACPProvider(create=True)
    communications = RecordingCommunications()
    app, capabilities, _ = _app(
        tmp_path, communications, configured.config.work_orders,
        WorkflowCatalog.from_graphs((graph,)), provider=provider,
        graph_runtime=runtime_before_row(),
    )
    body = json.dumps({"type": "event_callback", "event": {
        "type": "app_mention", "channel": "C", "user": "U", "ts": "1",
        "text": "<@BOT> new workorder please",
    }}).encode()
    with TestClient(app) as client:
        assert client.post("/api/slack/events", content=body, headers=_signed(body)).status_code == 200
        client.portal.call(app.state.slack_ingress.drain)
        result = provider.clients[0].result
        assert not result.get("isError"), result
        runs = client.portal.call(capabilities.state_store.list_runs)
        assert len(runs) == 1
        assert str(runs[0].workflow_id) == graph.graph_id
        assert runs[0].origin.thread_id == "1"
        snapshot = client.get(f"/graph/api/runs/{runs[0].run_id}").json()
        assert snapshot["values"]["inputs"] == {
            "implementation_runner": "codex", "review_runner": "claude",
        }
        expected = {
            "finished": "Work order finished.",
            "human_review": "Review complete and ready for your decision.",
            "failed": "Work order failed: review service unavailable",
        }[ending]
        async def wait_for_notification():
            async with asyncio.timeout(10):
                while not any(message.text == expected for _, message, _ in communications.posts):
                    await asyncio.sleep(0.01)
        client.portal.call(wait_for_notification)
        notifications = [
            (channel, message, thread) for channel, message, thread in communications.posts
            if message.text == expected
        ]
        assert len(notifications) == 1
        channel, message, thread = notifications[0]
        assert (channel, thread) == ("C", "1")
        assert message.mention == ("" if ending == "finished" else "U")
        assert any(str(runs[0].run_id) in link.url for link in message.links)
        assert any(message.text == "*work* started." for _, message, _ in communications.posts)
    assert any(message.links for _, message, _ in communications.posts)


def test_an_auto_approved_request_is_not_announced(tmp_path) -> None:
    """A question the run answers itself is not reported to the thread.

    One work order asks to run dozens of commands, and with `auto_approve` on
    every one of them is settled by the run. Announcing each as "needs your
    approval" would bury the requests that really are somebody's -- the plan
    below, which is still announced.
    """
    from contextlib import asynccontextmanager

    from starlette.testclient import TestClient

    from engine.domain import ApprovalKind
    from engine.graph_runtime import GraphId, NodeId
    from engine.runtime import WorkflowCatalog
    from engine.runtime.config import ApprovalConfig
    from graph_runtime_fakes import (
        Ask,
        AwaitSteering,
        ScriptedGraph,
        ScriptedGraphRuntime,
        ScriptedNode,
    )

    node = NodeId("implementation")
    graph = ScriptedGraph(
        GraphId("implementation-review-v1"),
        "Implementation review",
        (
            ScriptedNode(
                node,
                (
                    # Held here until the preference the app sets after starting
                    # the run has landed, so this is about what gets announced
                    # rather than a race with when auto-approve arrives.
                    AwaitSteering(),
                    Ask("Run git in the step's bound workspace"),
                    Ask("Approve the plan", kind=ApprovalKind.PLAN_APPROVAL),
                ),
            ),
        ),
    )
    runtime = ScriptedGraphRuntime(graph)

    @asynccontextmanager
    async def running(_app=None):
        yield runtime

    communications = RecordingCommunications()
    app, capabilities, _ = _app(
        tmp_path,
        communications,
        WorkOrdersConfig(repository="acme/api", workflow="implementation-review-v1"),
        WorkflowCatalog.from_graphs((graph,)),
        provider=FakeACPProvider(create=True),
        graph_runtime=running(),
        approval_policy=ApprovalConfig(auto_approve=True),
    )
    body = json.dumps({"type": "event_callback", "event": {
        "type": "app_mention", "channel": "C", "user": "U", "ts": "1",
        "text": "<@BOT> new workorder please",
    }}).encode()
    with TestClient(app) as client:
        assert client.post("/api/slack/events", content=body, headers=_signed(body)).status_code == 200
        client.portal.call(app.state.slack_ingress.drain)
        runs = client.portal.call(capabilities.state_store.list_runs)
        assert len(runs) == 1
        run_id = runs[0].run_id

        def said() -> list[str]:
            return [
                message.text for _, message, _ in communications.posts
                if isinstance(message, CommunicationsMessage)
            ]

        async def reach_the_plan() -> None:
            async with asyncio.timeout(10):
                while node not in (await runtime.snapshot(run_id)).auto_approve_nodes:
                    await asyncio.sleep(0.01)
                await runtime.steer(run_id, "carry on")
                while not any("Approve the plan" in text for text in said()):
                    await asyncio.sleep(0.01)

        client.portal.call(reach_the_plan)
        announced = said()

    assert "*implementation* needs your approval: Approve the plan" in announced
    assert not [text for text in announced if "Run git" in text]
