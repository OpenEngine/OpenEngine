import asyncio
import json
import sys
import shutil
from pathlib import Path

import pytest

from langgraph_acp import ACPAgentProvider, ACPPermissionRequest, ACPPermissionOutcome, StdioACPProvider
from langgraph_acp.readiness import AgentReadinessCache

FAKE = Path(__file__).with_name('fake_agent.py')
UNSUPPORTED = "The 'gpt-6.1-sol' model is not supported when using Codex with a ChatGPT account."


def provider(log: Path, **env: str) -> StdioACPProvider:
    return StdioACPProvider(name='codex', command=[sys.executable, str(FAKE)],
                            env={'FAKE_AGENT_LOG': str(log), **env})


def test_readiness_uses_real_adapter_session_and_coalesces_requests(tmp_path: Path) -> None:
    log = tmp_path / 'calls.jsonl'
    agent = provider(log)
    cache = AgentReadinessCache()

    async def scenario() -> None:
        results = await asyncio.gather(*(cache.check(agent) for _ in range(5)))
        assert all(r.ready for r in results)
        assert (await cache.check(agent)).ready
    asyncio.run(scenario())
    messages = [json.loads(line) for line in log.read_text().splitlines()]
    assert sum(m.get('method') == 'session/prompt' for m in messages) == 1
    request = next(m['params'] for m in messages if m.get('method') == 'session/new')
    assert request['mcpServers'] == []
    assert request['cwd'].split('/')[-1].startswith('engine-agent-probe-')
    assert not Path(request['cwd']).exists()


def test_readiness_reports_provider_rejection_instead_of_success(tmp_path: Path) -> None:
    response = json.dumps({'type': 'error', 'status': 400,
                           'error': {'type': 'invalid_request_error', 'message': UNSUPPORTED}})
    agent = provider(tmp_path / 'calls', FAKE_AGENT_RESPONSE='Warning: model metadata not found\n\n' + response)
    result = asyncio.run(AgentReadinessCache().check(agent))
    assert not result.ready and UNSUPPORTED in result.reason


def test_explicit_model_is_probed_and_not_replaced(tmp_path: Path) -> None:
    result = asyncio.run(AgentReadinessCache().check(provider(tmp_path / 'calls'), {'model': 'unavailable'}))
    assert not result.ready and 'Unknown model' in result.reason


def test_readiness_timeout_is_bounded(tmp_path: Path) -> None:
    agent = StdioACPProvider(name='slow', command=[sys.executable, str(FAKE), '--slow'])
    result = asyncio.run(AgentReadinessCache(timeout=.2).check(agent))
    assert not result.ready and 'timed out' in result.reason


def test_config_change_invalidates_probe_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    log = tmp_path / 'calls'
    agent = provider(log)
    cache = AgentReadinessCache()

    async def scenario() -> None:
        assert (await cache.check(agent)).ready
        (tmp_path / 'config.toml').write_text('model = "new-model"\n')
        assert (await cache.check(agent)).ready
    asyncio.run(scenario())
    assert sum(json.loads(line).get('method') == 'session/prompt' for line in log.read_text().splitlines()) == 2


@pytest.mark.parametrize('kind', ['codex', 'claude', 'opencode'])
def test_auto_discovery_skips_absent_clis_without_launching_adapter(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    from langgraph_acp import ClaudeACPProvider, CodexACPProvider, OpenCodeACPProvider
    from langgraph_acp import readiness
    monkeypatch.delenv('CODEX_PATH', raising=False)
    monkeypatch.setattr(shutil, 'which', lambda *args, **kwargs: None)
    agents: dict[str, ACPAgentProvider] = {'codex': CodexACPProvider(), 'claude': ClaudeACPProvider(), 'opencode': OpenCodeACPProvider()}
    reason = readiness.missing_runtime(agents[kind])
    assert reason is not None and 'not installed' in reason


def test_auto_discovery_honors_explicit_codex_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    from langgraph_acp import CodexACPProvider
    from langgraph_acp import readiness
    seen: list[str] = []
    def which(binary: str, *, path: str | None = None) -> str:
        seen.append(binary)
        return binary
    monkeypatch.setattr(shutil, 'which', which)
    assert readiness.missing_runtime(CodexACPProvider(env={'CODEX_PATH': '/custom/codex'})) is None
    assert seen == ['/custom/codex']


def test_readiness_never_uses_execution_permission_handlers(tmp_path: Path) -> None:
    async def should_not_ask(_request: ACPPermissionRequest) -> ACPPermissionOutcome:
        pytest.fail('preflight used an execution permission handler')
    agent = StdioACPProvider(name='agent', command=[sys.executable, str(FAKE), '--permission'],
                            permissions=should_not_ask)
    result = asyncio.run(AgentReadinessCache().check(agent))
    assert result.ready


def test_probe_shutdown_cancels_an_in_flight_session(tmp_path: Path) -> None:
    agent = StdioACPProvider(name='slow', command=[sys.executable, str(FAKE), '--slow'])
    async def scenario() -> None:
        cache = AgentReadinessCache()
        check = asyncio.create_task(cache.check(agent))
        while not cache._pending:
            await asyncio.sleep(0)
        await cache.aclose()
        with pytest.raises(asyncio.CancelledError):
            await check
        assert not cache._pending
    asyncio.run(scenario())
