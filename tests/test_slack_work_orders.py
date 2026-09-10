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
    RunRequested,
    RunState,
    StepId,
    StepSpec,
    TaskId,
    WorkflowId,
    WorkspaceId,
)
from engine.domain.chat import Message
from engine.ports import AgentTurn, Message as CommunicationsMessage, McpServerConfig
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


def _app(tmp_path, communications, work_orders: WorkOrdersConfig, catalog=None, provider=None, github_login_config=None, graph_runtime=None):
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
        workflow_runners=runners,
        review_runners=runners,
        workflow_catalog=(
            catalog if catalog is not None else WorkflowCatalog.from_definitions(())
        ),
        slack_credential_store=slack_store,
        github_login_config=github_login_config,
        public_url="https://engine.example",
        work_orders=work_orders,
        credential_store=MagicMock(),
        concierge_provider=provider or FakeACPProvider(),
        graph_runtime=graph_runtime,
    ), capabilities, slack_store


def _workflow_catalog():
    import openengine as oe
    from engine.runtime import WorkflowCatalog

    coder = oe.agent(id="coder", instructions="Implement it.")
    return WorkflowCatalog.from_definitions(
        [
            oe.workflow(
                id="implementation-review-v1",
                name="Implementation review",
                version="v1",
                steps=[
                    oe.agent_step(
                        id="implementation",
                        name="Implementation",
                        agent=coder,
                        prompt=oe.template("{task}", task=oe.task.prompt),
                        transitions={"*": oe.succeed()},
                    )
                ],
            )
        ]
    )


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


class CompletingMcpRunner:
    """A runner that completes each step through the real run-bound server.

    Enough of an agent to exercise the reporting path end to end: it posts one
    status, then completes, declaring `pr_url` on the step that has it.
    """

    permission_translator = UNCLASSIFIED_PERMISSION_TRANSLATOR

    def __init__(self, pull_request_url: str) -> None:
        self._pull_request_url = pull_request_url

    async def run_turn(self, *_args, **_kwargs):  # pragma: no cover
        raise AssertionError("this test drives the MCP path")

    async def run_turn_with_mcp(
        self,
        agent_run_id,
        profile,
        messages,
        mcp_server,
        workspace_id=None,
    ):
        from engine.domain import Message as ChatMessage

        outputs = (
            {"pr_url": self._pull_request_url}
            if "implementation" in str(agent_run_id)
            else {}
        )
        await _call_bound_tool(
            mcp_server, "update_status", {"status": "reading the code"}, "call-1"
        )
        await _call_bound_tool(
            mcp_server,
            "complete_step",
            {"outcome": "success", "summary": "Done.", "outputs": outputs},
            "call-2",
        )
        await asyncio.sleep(0)
        return AgentTurn(ChatMessage.assistant("Completed."))

    async def cancel(self, _agent_run_id) -> None:
        return None


async def _call_bound_tool(mcp_server, name, arguments, request_id):
    host = mcp_server.args[mcp_server.args.index("--host") + 1]
    port = int(mcp_server.args[mcp_server.args.index("--port") + 1])
    token = mcp_server.args[mcp_server.args.index("--token") + 1]
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(
        json.dumps(
            {
                "token": token,
                "request_id": request_id,
                "name": name,
                "arguments": arguments,
            }
        ).encode()
        + b"\n"
    )
    await writer.drain()
    response = json.loads(await reader.readline())
    writer.close()
    await writer.wait_closed()
    assert response["ok"] is True, response
    return response


class OneWorkspaceProvider:
    async def provision(self, repository: str, base_ref: str):
        from engine.ports import Workspace

        return Workspace(
            workspace_id=WorkspaceId("ws-1"),
            root_path="/tmp/ws-1",
            repository=repository,
            base_ref=base_ref,
        )


def _reporting_workflow(*, notification: bool = True):
    import openengine as oe

    coder = oe.agent(id="coder", instructions="Implement it.")
    reviewer = oe.agent(id="reviewer", instructions="Review it.")
    implementation = oe.result("implementation")
    return oe.workflow(
        id="implementation-review-v1",
        name="Implementation review",
        version="v1",
        workspace=oe.workspace(base_ref="origin/main"),
        steps=[
            oe.agent_step(
                id="implementation",
                name="Implementation",
                agent=coder,
                prompt=oe.template("{task}", task=oe.task.prompt),
                required_outputs=["pr_url"],
                workspace_access="write",
                transitions={"success": oe.goto("review"), "*": oe.fail()},
            ),
            oe.agent_step(
                id="review",
                name="Review",
                agent=reviewer,
                prompt=oe.template("Review {pr}", pr=implementation.outputs),
                workspace_access="read",
                transitions={"*": oe.goto("human-review")},
            ),
            oe.human_review_step(
                id="human-review",
                name="Human review",
                title=oe.template("Review {task_id}", task_id=oe.task.id),
                summary=oe.template("done"),
                approved=oe.succeed(),
                rejected=oe.fail(),
                notification=oe.slack_notification() if notification else None,
            ),
        ],
    )


PULL_REQUEST = "https://example.invalid/pr/9"
DRIVEN_RUN = RunId("run-1")


def _drive_to_human_review(definition) -> RecordingCommunications:
    """Run a work order with an origin until it parks on a human decision."""
    from engine.runtime import Capabilities, WorkflowCatalog, WorkflowExecutor

    communications = RecordingCommunications()
    store = InMemoryStateStore()
    runner = CompletingMcpRunner(PULL_REQUEST)
    capabilities = Capabilities(
        workflow_runtime=object(),
        source_control=object(),
        agent_runner=runner,
        communications=communications,
        workspace_provider=OneWorkspaceProvider(),
        state_store=store,
    )
    executor = WorkflowExecutor(
        capabilities,
        {"default": runner},
        review_runners={"default": runner},
        catalog=WorkflowCatalog.from_definitions([definition]),
        public_url="https://engine.example",
    )
    run_id = DRIVEN_RUN
    origin = RunOrigin(channel="C123", thread_id="1700.0001", author="U777")

    async def scenario() -> None:
        await store.save(
            RunState(
                run_id=run_id,
                task_id=TaskId("task-1"),
                workflow_id=definition.workflow_id,
                prompt="add a health endpoint",
                repository="acme/api",
                origin=origin,
            )
        )
        await executor.start(
            RunRequested(
                run_id=run_id,
                task_id=TaskId("task-1"),
                prompt="add a health endpoint",
                repository="acme/api",
                workflow_id=definition.workflow_id,
            ),
            "default",
        )

    asyncio.run(scenario())
    return communications


def test_a_work_order_reports_its_whole_life_in_the_thread() -> None:
    pull_request = PULL_REQUEST
    communications = _drive_to_human_review(_reporting_workflow())

    said = [message.text for _channel, message, _thread in communications.posts]
    assert all(thread == "1700.0001" for _c, _m, thread in communications.posts)
    assert "*Implementation* started." in said
    assert "*Implementation*: reading the code" in said
    assert any(text.startswith("*Implementation* complete.") for text in said)
    # The review stage announces itself, which is the point of announcing on
    # entry rather than only on ending.
    assert "*Review* started." in said

    completion = next(
        message
        for _channel, message, _thread in communications.posts
        if message.text.startswith("*Implementation* complete.")
    )
    assert [link.url for link in completion.links] == [
        pull_request,
        f"https://engine.example/runs/{DRIVEN_RUN}",
    ]

    # The last word is addressed to whoever asked, because it is their decision
    # the run is now waiting on.
    ready = communications.posts[-1][1]
    assert ready.mention == "U777"
    assert "ready for your decision" in ready.text.lower()
    assert pull_request in [link.url for link in ready.links]


def test_the_author_is_pinged_even_without_an_operator_notification() -> None:
    """`notification` configures the operators' channel, not this.

    A workflow that omits it must still ping whoever asked, or their thread
    reports the review complete and then goes silent forever with the run
    parked on a decision nobody was told about.
    """
    communications = _drive_to_human_review(_reporting_workflow(notification=False))

    ready = communications.posts[-1][1]
    assert ready.mention == "U777"
    assert "ready for your decision" in ready.text.lower()
    assert PULL_REQUEST in [link.url for link in ready.links]


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


def test_mcp_initialize_returns_server_protocol_version() -> None:
    """The server always returns its own version, not the client's."""
    from engine.slack_concierge.slack_egress import _mcp_response, _PROTOCOL_VERSION

    async def scenario() -> None:
        result = await _mcp_response(
            "127.0.0.1", 0, "tok",
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "1999-01-01",
                "clientInfo": {"name": "test", "version": "1"},
            }},
        )
        assert result is not None
        assert result["result"]["protocolVersion"] == _PROTOCOL_VERSION

    asyncio.run(scenario())


def test_mcp_tools_list_returns_create_workorder() -> None:
    from engine.slack_concierge.slack_egress import _mcp_response

    async def scenario() -> None:
        result = await _mcp_response(
            "127.0.0.1", 0, "tok",
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        assert result is not None
        tools = result["result"]["tools"]
        assert len(tools) == 1
        assert tools[0]["name"] == "create_workorder"

    asyncio.run(scenario())


def test_mcp_notifications_are_swallowed() -> None:
    from engine.slack_concierge.slack_egress import _mcp_response

    async def scenario() -> None:
        result = await _mcp_response(
            "127.0.0.1", 0, "tok",
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert result is None

    asyncio.run(scenario())


def test_mcp_unknown_method_returns_error() -> None:
    from engine.slack_concierge.slack_egress import _mcp_response

    async def scenario() -> None:
        result = await _mcp_response(
            "127.0.0.1", 0, "tok",
            {"jsonrpc": "2.0", "id": 3, "method": "resources/list"},
        )
        assert result is not None
        assert result["error"]["code"] == -32601

    asyncio.run(scenario())




class FakeACPProvider:
    name = "fake"

    def __init__(self, text="Hi, how can I help?", fail=False, create=False, fail_after_create=False):
        self.text, self.fail, self.create = text, fail, create
        self.clients = []
        self.fail_after_create = fail_after_create

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
                    self.result = await call_mcp(self.config)
                    if provider.fail_after_create:
                        raise RuntimeError("failed after accepting work")
                yield ACPEvent(agent="fake", type=ACPEventType.MESSAGE_DELTA,
                               data={"content": {"type": "text", "text": provider.text}})
            async def close(self):
                self.closed = True
        client = Client()
        self.clients.append(client)
        return client


async def call_mcp(config):
    """Real stdio child -> TCP broker -> injected host callback."""
    process = await asyncio.create_subprocess_exec(
        config["command"], *config["args"], stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "unsupported"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "create_workorder", "arguments": {"prompt": "Implement it"}}},
    ]
    stdout, stderr = await process.communicate("".join(json.dumps(r) + "\n" for r in requests).encode())
    assert process.returncode == 0, stderr.decode()
    responses = [json.loads(line) for line in stdout.splitlines()]
    assert len(responses) == 3
    assert responses[0]["result"]["protocolVersion"] == "2025-06-18"
    assert responses[1]["result"]["tools"][0]["name"] == "create_workorder"
    return responses[2]["result"]


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
def test_thread_reply_creates_workorder_through_stdio_mcp(tmp_path, monkeypatch, fail_after_create):
    from starlette.testclient import TestClient
    from engine.runtime import WorkflowExecutor
    posts_at_start = []
    async def no_drive(self, event, runner_name):
        posts_at_start.append(list(communications.posts))
    monkeypatch.setattr(WorkflowExecutor, "start", no_drive)
    provider = FakeACPProvider(create=True, fail_after_create=fail_after_create)
    communications = RecordingCommunications()
    app, capabilities, _ = _app(tmp_path, communications,
        WorkOrdersConfig(repository="acme/api", workflow="implementation-review-v1", runner="default"),
        _workflow_catalog(), provider=provider)
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
    assert len(posts_at_start) == 1
    announcements = [m for _, m, _ in posts_at_start[0] if m.text.startswith("Started a work order")]
    assert len(announcements) == 1
    assert announcements[0].links
    if not fail_after_create:
        assert posts_at_start[0][-2][1].text == provider.text
    assert any(m.links for _, m, _ in communications.posts)
    assert all(thread == "1" for _, _, thread in communications.posts)
    # The work-order announcement (with the link) must follow the conversational
    # reply so that messages appear in the expected order in the thread.
    link_indices = [i for i, (_, m, _) in enumerate(communications.posts) if m.links]
    reply_indices = [i for i, (_, m, _) in enumerate(communications.posts) if not m.links]
    assert reply_indices and link_indices
    assert fail_after_create or max(reply_indices) < min(link_indices), (
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


def test_slack_starts_configured_graph_with_input_defaults(tmp_path):
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
    builder.add_edge("work", END)
    graph = graph_workflow(
        builder, id="implementation-review-rerank", name="Implementation review rerank",
        inputs=(WorkflowInput("implementation_runner", "Implementation runner", "codex"),
                WorkflowInput("review_runner", "Review runner", "claude")),
    )
    provider = FakeACPProvider(create=True)
    communications = RecordingCommunications()
    app, capabilities, _ = _app(
        tmp_path, communications, configured.config.work_orders,
        WorkflowCatalog.from_definitions((), (graph,)), provider=provider,
        graph_runtime=sqlite_runtime((graph,), tmp_path / "graph"),
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
    assert any(message.links for _, message, _ in communications.posts)
