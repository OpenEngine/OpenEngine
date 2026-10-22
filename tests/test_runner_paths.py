"""`[runners.<agent>].path` reaches every place Engine starts that agent."""

import langgraph_acp.agent
import pytest
from langgraph_acp import default_registry

from engine.adapters.agent_runner.acp import agent_provider, runner_binary
from engine.apps.control_server.composition import Settings as ControlServerSettings
from engine.apps.control_server.composition import build_capabilities as build_control_server
from engine.apps.web import __main__ as web_main
from engine.apps.web import api
from engine.apps.web.composition import (
    Settings,
    added_agent,
    build_capabilities,
    build_read_only_runners,
    build_runners,
    builtin_agents,
)
from engine.apps.worker.composition import Settings as WorkerSettings
from engine.apps.worker.composition import build_capabilities as build_worker
from engine.cli import starters
from engine.graph_service.store import AgentRow
from engine.runtime import EngineConfig
from engine.scoper import milestone_scoper


@pytest.fixture
def stub(tmp_path, monkeypatch):
    """A stub Codex named by `[runners.codex].path`, beside another on `PATH`."""
    monkeypatch.setenv("CODEX_PATH", "/environment/codex")
    monkeypatch.setenv("ENGINE_CODEX_PATH", "/environment/engine-codex")
    monkeypatch.setattr(langgraph_acp.agent, "_DEFAULT", None)
    codex = tmp_path / "stub-codex"
    codex.write_text("#!/bin/sh\nexit 0\n")
    codex.chmod(0o755)
    return str(codex)


def _config(stub: str) -> EngineConfig:
    return EngineConfig(runner_paths={"codex": stub})


def test_the_graph_service_registry_runs_the_configured_codex(stub) -> None:
    assert builtin_agents(_config(stub)).resolve("codex").env == {"CODEX_PATH": stub}


def test_runner_binary_names_the_configured_binary_or_the_pinned_package(stub, monkeypatch) -> None:
    from langgraph_acp.providers.claude import CLAUDE_ACP_VERSION
    from langgraph_acp.providers.opencode import OPENCODE_VERSION

    monkeypatch.setattr("engine.adapters.agent_runner.acp.binary_version", lambda path: f"{path} 1.0")
    config = EngineConfig(runner_paths={"codex": stub, "claude": "/opt/claude"})
    assert runner_binary(config, "codex") == (stub, f"{stub} 1.0")
    assert runner_binary(config, "claude") == ("/opt/claude", "/opt/claude 1.0")
    assert runner_binary(EngineConfig(), "claude") == (
        "npx @agentclientprotocol/claude-agent-acp", CLAUDE_ACP_VERSION,
    )
    assert runner_binary(EngineConfig(), "opencode") == ("npx opencode-ai", OPENCODE_VERSION)


def test_an_added_codex_agent_runs_the_configured_codex(stub) -> None:
    row = AgentRow(name="codex-fast", kind="codex", model="", url="", created_at="", updated_at="")

    assert added_agent(row, _config(stub)).env == {"CODEX_PATH": stub}


def test_the_web_runners_run_the_configured_codex_under_the_policy(stub, tmp_path) -> None:
    settings = Settings(sqlite_path=str(tmp_path / "c.sqlite3"), engine_config=_config(stub))
    capabilities = build_capabilities(settings)
    try:
        runners = (
            capabilities.agent_runner,
            build_runners(settings)["codex"],
            build_read_only_runners(settings)["codex"],
        )
    finally:
        capabilities.state_store.close()

    assert {runner.provider.env["ENGINE_CODEX_PATH"] for runner in runners} == {stub}


def test_the_worker_runs_the_configured_codex(stub) -> None:
    runner = build_worker(WorkerSettings(engine_config=_config(stub))).agent_runner

    assert runner.provider.env["ENGINE_CODEX_PATH"] == stub


def test_the_control_server_runs_the_configured_codex(stub) -> None:
    runner = build_control_server(ControlServerSettings(engine_config=_config(stub))).agent_runner

    assert runner.provider.env["ENGINE_CODEX_PATH"] == stub


def test_loops_and_concierges_run_the_configured_codex(stub, monkeypatch, web_app) -> None:
    providers: dict[str, object] = {}

    def recording(name, real):
        def build(*args, **kwargs):
            providers[name] = kwargs["provider"] if "provider" in kwargs else args[2]
            return real(*args, **kwargs)
        return build

    monkeypatch.setattr(api, "LoopRunner", recording("loops", api.LoopRunner))
    monkeypatch.setattr(api, "SlackConcierge", recording("slack", api.SlackConcierge))
    monkeypatch.setattr(api, "GithubConcierge", recording("github", api.GithubConcierge))

    web_app(runners={"codex": object()}, agent_config=_config(stub))

    assert {name: provider.env for name, provider in providers.items()} == {
        "loops": {"CODEX_PATH": stub},
        "slack": {"CODEX_PATH": stub},
        "github": {"CODEX_PATH": stub},
    }


def test_milestone_scoping_runs_the_provider_it_is_given(stub) -> None:
    scoper = milestone_scoper(
        agent_provider(_config(stub), "codex"), working_directory=".", timeout_seconds=None
    ).scoper

    assert scoper.agent == "codex"
    assert scoper.registry is not None
    assert scoper.registry.resolve("codex").env == {"CODEX_PATH": stub}


def test_startup_configures_the_default_registry_starters_and_open_verify(stub, tmp_path) -> None:
    """A node given no registry, a starter graph, and the `ov` the daemon starts."""
    config = tmp_path / "engine.toml"
    config.write_text(f'[runners.codex]\npath = "{stub}"\n')

    web_main.read_configuration(config)
    namespace: dict[str, object] = {}
    exec(compile(starters.source("review"), "review_graph.py", "exec"), namespace)

    assert default_registry().resolve("codex").env == {"CODEX_PATH": stub}
    assert namespace["AGENTS"].resolve("codex").env == {"CODEX_PATH": stub}  # type: ignore[attr-defined]
    assert web_main.os.environ["CODEX_PATH"] == stub


def _paths(tmp_path) -> EngineConfig:
    return EngineConfig(runner_paths={"claude": str(tmp_path / "claude"), "opencode": str(tmp_path / "opencode")})


def test_the_factory_runs_the_configured_claude_and_opencode(tmp_path) -> None:
    config = _paths(tmp_path)

    assert agent_provider(config, "claude").env == {"CLAUDE_CODE_EXECUTABLE": str(tmp_path / "claude")}
    assert agent_provider(config, "opencode").command == (str(tmp_path / "opencode"), "acp")
    assert agent_provider(config, "opencode", command=("fake",)).command == ("fake",)


def test_the_web_runners_run_the_configured_claude_and_opencode(tmp_path) -> None:
    settings = Settings(sqlite_path=str(tmp_path / "c.sqlite3"), engine_config=_paths(tmp_path))

    for runners in (build_runners(settings), build_read_only_runners(settings)):
        assert runners["claude"].provider.env["CLAUDE_CODE_EXECUTABLE"] == str(tmp_path / "claude")
        assert runners["opencode"].provider.command == (str(tmp_path / "opencode"), "acp")
        assert "OPENCODE_CONFIG_CONTENT" in runners["opencode"].provider.env


def test_startup_exports_the_configured_claude_for_open_verify(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CLAUDE_CODE_EXECUTABLE", raising=False)
    monkeypatch.setattr(langgraph_acp.agent, "_DEFAULT", None)
    claude = tmp_path / "claude"
    config = tmp_path / "engine.toml"
    config.write_text(f'[runners.claude]\npath = "{claude}"\n')

    web_main.read_configuration(config)

    assert web_main.os.environ["CLAUDE_CODE_EXECUTABLE"] == str(claude)
    assert default_registry().resolve("claude").env == {"CLAUDE_CODE_EXECUTABLE": str(claude)}
