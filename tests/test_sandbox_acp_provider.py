"""ACP crosses real pipes through each backend; no mocked protocol client."""

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shutil
import sys

import pytest

from engine.adapters.sandbox.process import ProcessSandbox
from engine.adapters.sandbox.smolvm import SmolvmSandbox
from engine.graph_runtime import EventLog, GraphId
from engine.graph_runtime_langgraph import (
    ACPNode, LangGraphDefinition, LangGraphRuntime, SandboxACPProvider,
    SandboxConnectContext,
)
from engine.graph_runtime_langgraph.sandbox import sandbox_provider
from engine.ports import SandboxSpec
from engine.ports.sandbox import HOST_ENV_ALLOWLIST
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph_acp import (
    ACPAgentRegistry, ACPConnectContext, ACPConnectionError, ACPContinuation,
    ACPSessionError, ClaudeACPProvider, CodexACPProvider, StdioACPProvider, ACPEventType,
    resume_continuation,
)


@pytest.fixture(params=["smolvm-fake"])
def backend(request):
    if request.param == "process":
        return ProcessSandbox()
    launcher, _ = request.getfixturevalue("smolvm_cli")
    return SmolvmSandbox(image="fixture", executable=str(launcher))


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    shutil.copy2(Path(__file__).with_name("sandbox_acp_agent.py"), root / "agent.py")
    (root / "input.txt").write_text("workspace-marker")
    return root


async def run_node(node, *, backend=None, timeout=45, model_credentials=None):
    builder = StateGraph(dict)
    builder.add_node("agent", node)
    builder.add_edge(START, "agent")
    builder.add_edge("agent", END)
    runtime = LangGraphRuntime(LangGraphDefinition(
        graph_id=GraphId("sandbox"), name="Sandbox test",
        graph=builder.compile(checkpointer=InMemorySaver()),
    ), sandbox=backend, model_credentials=model_credentials)
    events = EventLog()
    runtime.observe(events.append)
    try:
        run = await runtime.start(GraphId("sandbox"), {})
        async with asyncio.timeout(timeout):
            async for event in events.stream(run.run_id):
                if event.kind.value in {"run.finished", "run.failed"}:
                    assert event.kind.value == "run.finished", event.payload
                    break
        snapshot = await runtime.snapshot(run.run_id)
        return snapshot.values["agent"]
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("provider_type", [CodexACPProvider, ClaudeACPProvider])
def test_agent_node_runs_inside_sandbox(backend, workspace, provider_type, monkeypatch, smolvm_cli):
    monkeypatch.setenv("GITHUB_TOKEN", "unselected-forge-key")
    monkeypatch.setenv("OPENAI_API_KEY", "unselected-model-key")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/host/ssh-agent")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/host/claude-account")
    provider_value = "private-provider-value"
    agent = provider_type(command=(sys.executable, "agent.py", "--large"), env={"EXPLICIT_SETTING": provider_value})
    resolved = []

    def spec(state):
        resolved.append(state)
        return SandboxSpec(secrets={"SELECTED_KEY": "test-key"})

    node = ACPNode(
        agent=agent.name, registry=ACPAgentRegistry([agent]), cwd=str(workspace),
        prompt="Read input.txt", sandbox_spec=spec,
    )
    result = json.loads(asyncio.run(run_node(node, backend=backend)))
    assert result["marker"] == "workspace-marker"
    assert result["large"] == "x" * 100_000
    assert result["cwd"] != str(workspace)
    assert not Path(result["cwd"]).exists(), "sandbox must be destroyed after the node closes"
    managed = {"GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL",
        "CODEX_HOME" if provider_type is CodexACPProvider else "CLAUDE_CONFIG_DIR"}
    assert set(result["env"]) <= HOST_ENV_ALLOWLIST | {"EXPLICIT_SETTING", "SELECTED_KEY"} | managed
    assert result["env"]["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert result["env"]["HOME"] != os.environ.get("HOME")
    assert result["env"]["EXPLICIT_SETTING"] == provider_value
    assert result["env"]["SELECTED_KEY"] == "test-key"
    assert len(resolved) == 1
    assert (workspace / "input.txt").read_text() == "workspace-marker"
    _, machines = smolvm_cli
    commands = (machines / "commands.jsonl").read_text()
    assert provider_value not in commands
    assert "--secret-env" in commands
    assert not list(machines.glob("oe-sandbox-*"))


def test_shared_provider_keeps_invocations_separate(backend, workspace, tmp_path):
    provider = SandboxACPProvider(name="test", command=(sys.executable, "agent.py"), backend=backend)
    second = tmp_path / "other"
    shutil.copytree(workspace, second)

    async def invoke(root, secret):
        context = SandboxConnectContext(
            workspace=str(root), run_id=secret, node_id="worker",
            sandbox_spec=SandboxSpec(secrets={"SELECTED_KEY": secret}),
        )
        client = await provider.connect(context=context)
        try:
            session = await client.new_session(cwd=str(root))
            text = ""
            async for event in session.prompt("hello"):
                if event.type == ACPEventType.MESSAGE_DELTA:
                    text += event.data["content"]["text"]
            return json.loads(text)
        finally:
            await client.close()

    async def run():
        return await asyncio.gather(invoke(workspace, "first"), invoke(second, "second"))

    first, other = asyncio.run(run())
    assert first["cwd"] != other["cwd"]
    assert first["env"]["SELECTED_KEY"] == "first"
    assert other["env"]["SELECTED_KEY"] == "second"
    assert provider.env == {}


def test_failed_handshake_cleans_sandbox(backend, workspace):
    entered = []

    class RecordingBackend:
        @asynccontextmanager
        async def create(self, spec):
            async with backend.create(spec) as sandbox:
                entered.append(await sandbox.working_directory())
                yield sandbox

    async def run():
        provider = SandboxACPProvider(
            name="broken", backend=RecordingBackend(),
            command=(sys.executable, "agent.py", "--exit-init"),
        )
        with pytest.raises(ACPConnectionError, match="initialization failed"):
            await provider.connect(context=ACPConnectContext(workspace=str(workspace)))

    asyncio.run(asyncio.wait_for(run(), 30))
    assert entered and not Path(entered[0]).exists()


@pytest.mark.parametrize("mode", ["cancel", "timeout"])
def test_interrupted_handshake_cleans_sandbox(backend, workspace, mode):
    entered = []
    started = None

    class RecordingBackend:
        @asynccontextmanager
        async def create(self, spec):
            async with backend.create(spec) as sandbox:
                entered.append(await sandbox.working_directory())
                started.set()
                yield sandbox

    async def run():
        nonlocal started
        started = asyncio.Event()
        provider = SandboxACPProvider(
            name="blocked", backend=RecordingBackend(),
            command=(sys.executable, "agent.py", "--hang-init"),
        )
        context = SandboxConnectContext(
            workspace=str(workspace),
            sandbox_spec=SandboxSpec(timeout=0.2 if mode == "timeout" else None),
        )
        task = asyncio.create_task(provider.connect(context=context))
        await started.wait()
        if mode == "cancel":
            await asyncio.sleep(0.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(ACPConnectionError):
                await task

    asyncio.run(asyncio.wait_for(run(), 30))
    assert entered and not Path(entered[0]).exists()


def test_disposable_session_load_is_explicitly_rejected(backend, workspace):
    provider = SandboxACPProvider(name="test", command=(sys.executable, "agent.py"), backend=backend)

    async def run():
        with pytest.raises(ACPSessionError, match="not persisted"):
            await resume_continuation(
                ACPContinuation(agent="test", session_id="previous-session"),
                registry=ACPAgentRegistry([provider]),
                context=ACPConnectContext(workspace=str(workspace)),
            )

    asyncio.run(run())


def test_default_guest_commands_are_installed_binaries():
    for provider, executable in [(CodexACPProvider(), "codex-acp"), (ClaudeACPProvider(), "claude-agent-acp")]:
        adapted = sandbox_provider(provider, ProcessSandbox())
        assert adapted.command == (executable,)
        assert adapted.name == provider.name


def test_provider_requires_an_explicit_workspace():
    async def run():
        provider = SandboxACPProvider(name="test", command=("unused",), backend=ProcessSandbox())
        with pytest.raises(ACPConnectionError, match="workspace"):
            await provider.connect()

    asyncio.run(run())


def test_runtime_open_passes_the_same_fields_on_fresh_and_resume(workspace, monkeypatch):
    from types import SimpleNamespace
    import engine.graph_runtime_langgraph.acp as acp

    seen = []

    class RecordingProvider(StdioACPProvider):
        async def connect(self, *, context=None):
            seen.append(context)
            return await super().connect(context=context)

    provider = RecordingProvider(name="test", command=(sys.executable, "agent.py"), cwd=str(workspace))
    spec = SandboxSpec(image="chosen-image", secrets={"SELECTED_KEY": "not-in-continuation"})
    monkeypatch.setattr(acp, "current_execution", lambda: SimpleNamespace(
        runtime=SimpleNamespace(sandbox=None), run_id="run-one", node_id="node-one", execution_id="exec-one",
    ))
    node = ACPNode(agent="test", registry=ACPAgentRegistry([provider]), cwd=str(workspace))

    async def run():
        client, session = await node._open(None, str(workspace), (), sandbox_spec=spec)
        continuation = ACPContinuation(agent="test", session_id=session.session_id)
        await client.close()
        client, _ = await node._open(continuation, str(workspace), (), sandbox_spec=spec)
        await client.close()
        assert "not-in-continuation" not in json.dumps(continuation.to_dict())

    asyncio.run(run())
    assert len(seen) == 2
    assert seen[0] == seen[1] == SandboxConnectContext(
        workspace=str(workspace), run_id="run-one", node_id="node-one", execution_id="exec-one", session_key="node-one", sandbox_spec=spec,
    )


def test_host_stdio_mcp_is_rejected_before_sandbox_launch(workspace, monkeypatch):
    from types import SimpleNamespace
    import engine.graph_runtime_langgraph.acp as acp

    monkeypatch.setattr(acp, "current_execution", lambda: SimpleNamespace(
        runtime=SimpleNamespace(sandbox=ProcessSandbox()), run_id="run", node_id="node", execution_id="exec",
    ))
    node = ACPNode(agent="test", registry=ACPAgentRegistry([
        StdioACPProvider(name="test", command=(sys.executable, "agent.py")),
    ]), cwd=str(workspace))

    async def run():
        with pytest.raises(ACPConnectionError, match="trusted sandbox bridge"):
            await node._open(None, str(workspace), ({"name": "host-tool", "command": "/host/python"},))

    asyncio.run(run())


def test_live_prompt_cancellation_releases_sandbox(backend, workspace):
    async def run():
        provider = SandboxACPProvider(
            name="blocked", backend=backend,
            command=(sys.executable, "agent.py", "--hang-prompt"),
        )
        client = await provider.connect(context=ACPConnectContext(workspace=str(workspace)))
        session = await client.new_session()

        async def speak():
            async for _ in session.prompt("hello"):
                pass

        speaking = asyncio.create_task(speak())
        await asyncio.sleep(0.1)
        speaking.cancel()
        with pytest.raises(asyncio.CancelledError):
            await speaking
        # Concurrent closes must await the same teardown, not return while a
        # different caller still owns a running VM or process.
        await asyncio.gather(client.close(), client.close())

    asyncio.run(asyncio.wait_for(run(), 30))


def test_local_launch_allowlists_match():
    from langgraph_acp.environment import HOST_ENV_ALLOWLIST as ACP_ALLOWLIST
    assert HOST_ENV_ALLOWLIST == ACP_ALLOWLIST


def test_runtime_selected_backend_replaces_provider_backend():
    original, selected = ProcessSandbox(), ProcessSandbox()
    provider = SandboxACPProvider(name="test", command=("agent",), backend=original)
    adapted = sandbox_provider(provider, selected)
    assert adapted.backend is selected
    assert provider.backend is original


@pytest.mark.parametrize('cleanup_cancelled', [False, True])
def test_failed_connection_keeps_primary_error_through_cancelled_cleanup(tmp_path, cleanup_cancelled):
    async def exercise():
        cleaning = asyncio.Event()
        release = asyncio.Event()
        primary = LookupError('synthetic-launch-failure')
        cleanup_error = asyncio.CancelledError() if cleanup_cancelled else RuntimeError('synthetic-cleanup-failure')
        class BrokenSandbox:
            async def working_directory(self):
                return '/workspace'
            async def exec(self, argv):
                raise primary
        class Backend:
            @asynccontextmanager
            async def create(self, spec):
                try:
                    yield BrokenSandbox()
                finally:
                    cleaning.set()
                    await release.wait()
                    raise cleanup_error
        provider = SandboxACPProvider(name='broken', command=('agent',), backend=Backend())
        connecting = asyncio.create_task(provider.connect(context=ACPConnectContext(workspace=str(tmp_path))))
        await cleaning.wait()
        for _ in range(2):
            connecting.cancel()
            await asyncio.sleep(0)
            assert not connecting.done()
        release.set()
        with pytest.raises(LookupError) as error:
            await connecting
        assert error.value is primary
        assert any('sandbox cleanup failed' in note for note in primary.__notes__)
    asyncio.run(exercise())
