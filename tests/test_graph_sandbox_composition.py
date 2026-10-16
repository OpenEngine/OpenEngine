"""Deployment configuration must reach the graph runtime, not just capabilities."""

import asyncio
from types import SimpleNamespace

import pytest

from engine.adapters.sandbox.smolvm import SmolvmSandbox
from engine.apps.web.composition import build_graph_runtime
from engine.runtime.model_credentials import host_model_credential


@pytest.mark.parametrize("backend", ["process", "smolvm"])
def test_configured_backend_reaches_graph_runtime(tmp_path, backend, request):
    if backend == "smolvm":
        request.getfixturevalue("smolvm_cli")
    image = str(tmp_path / "guest.tar")
    settings = SimpleNamespace(
        graph_state_directory=str(tmp_path / "graph-state"),
        engine_config=SimpleNamespace(
            public_url="", sandbox=SimpleNamespace(backend=backend, image=image),
        ),
    )

    async def run():
        # Opening a runtime must not boot a VM. Provisioning belongs to an
        # actual agent connection, after its per-node workspace is resolved.
        async with build_graph_runtime(settings, []) as runtime:
            assert runtime.model_credentials is host_model_credential
            if backend == "smolvm":
                assert isinstance(runtime.sandbox, SmolvmSandbox)
                assert runtime.sandbox.image == image
            else:
                assert runtime.sandbox is None

    asyncio.run(run())


@pytest.mark.parametrize('reason', ['SmolVM requires Apple Silicon on macOS', 'KVM is unavailable', 'smolvm is not on PATH'])
def test_default_backend_fails_clearly_instead_of_launching_on_host(tmp_path, monkeypatch, reason):
    import engine.adapters.sandbox.smolvm as adapter
    from engine.runtime.config import parse_engine_config
    monkeypatch.setattr(adapter, 'detect_support', lambda _: adapter.SmolvmSupport(False, reason))
    settings = SimpleNamespace(graph_state_directory=str(tmp_path / 'state'), engine_config=parse_engine_config({}))
    async def open_runtime():
        async with build_graph_runtime(settings, []):
            pytest.fail('unsupported defaults must not open a host graph runtime')
    with pytest.raises(RuntimeError, match='engine vm use process') as error:
        asyncio.run(open_runtime())
    assert reason in str(error.value)


def test_default_backend_keeps_scoper_and_concierge_runners_on_host(tmp_path):
    from engine.apps.web.composition import Settings, build_capabilities, build_runners, build_read_only_runners
    from langgraph_acp import CodexACPProvider, ClaudeACPProvider, OpenCodeACPProvider
    settings = Settings(sqlite_path=str(tmp_path / 'state.db'),
        codex_acp_command=('host-codex',), claude_acp_command=('host-claude',), opencode_acp_command=('host-opencode',))
    assert settings.engine_config.sandbox.backend == 'smolvm'
    scoper = build_capabilities(settings).agent_runner
    assert isinstance(scoper.provider, CodexACPProvider)
    assert tuple(scoper.provider.command) == ('host-codex',)
    for runners in (build_runners(settings), build_read_only_runners(settings)):
        for name, cls in [('codex', CodexACPProvider), ('claude', ClaudeACPProvider), ('opencode', OpenCodeACPProvider)]:
            assert isinstance(runners[name].provider, cls)
            assert tuple(runners[name].provider.command) == (f'host-{name}',)
