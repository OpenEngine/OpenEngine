import asyncio
import subprocess
import tomllib
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from engine.apps.web import onboarding as onboard
from engine.runtime.repositories import RepositoryRegistry


@pytest.mark.parametrize('reference', [
    'https://github.com/owner/repo', 'https://github.com/owner/repo.git',
    'git@github.com:owner/repo.git', 'owner/repo',
])
def test_repository_references(reference):
    assert onboard.repository_name(reference) == 'owner/repo'


@pytest.mark.parametrize('reference', [
    'gh repo clone owner/repo', 'gh repo clone git@github.com:owner/repo.git',
    'gh repo clone https://github.com/owner/repo',
])
def test_gh_repo_clone_prefix_stripped(reference):
    assert onboard.repository_name(reference) == 'owner/repo'


@pytest.mark.parametrize('reference', [
    'https://gitlab.com/owner/repo', 'git@evil.example:owner/repo.git',
    'https://github.com/owner/repo/tree/main', '../repo', 'owner', 'owner/repo?query',
])
def test_invalid_references(reference):
    with pytest.raises(onboard.OnboardingError):
        onboard.repository_name(reference)


def test_atomic_idempotent_config_preserves_content(tmp_path):
    config = tmp_path / 'engine.toml'
    original = '# settings\n[server]\nport = 4364 # keep\n\n[repos]\nexisting = "/existing" # checkout\n'
    config.write_text(original)
    onboard.persist_repository(config, 'owner/repo', str(tmp_path / 'repo'), 'disconnected')
    first = config.read_text()
    assert original in first
    assert tomllib.loads(first)['repo_modes'] == {'owner/repo': 'disconnected'}
    stamp = config.stat().st_mtime_ns
    onboard.persist_repository(config, 'owner/repo', str(tmp_path / 'repo'), 'connected')
    assert config.read_text() == first
    assert config.stat().st_mtime_ns == stamp


def test_config_preserves_inline_tables(tmp_path):
    config = tmp_path / 'engine.toml'
    config.write_text('repos = {existing = "/existing"} # inline\n')
    onboard.persist_repository(config, 'owner/repo', str(tmp_path / 'repo'), 'connected')
    assert '# inline' in config.read_text()
    assert tomllib.loads(config.read_text())['repos']['existing'] == '/existing'


def test_new_config_uses_machine_path(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'xdg'))
    service = onboard.RepositoryOnboarding(RepositoryRegistry(projects={}), None, AsyncMock())
    assert service.config_path == tmp_path / 'xdg/openengine/engine.toml'
    onboard.persist_repository(service.config_path, 'owner/repo', str(tmp_path / 'repo'), 'connected')
    assert tomllib.loads(service.config_path.read_text())['repos']['owner/repo'] == str(tmp_path / 'repo')


@pytest.mark.parametrize('failure', ['git', 'github_ssh', 'agent'])
def test_preflight_failures_leave_destination_and_config_untouched(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(onboard.shutil, 'which', lambda binary: None if failure == 'git' and binary == 'git' else binary)
    monkeypatch.setattr(onboard, '_run', lambda args: subprocess.CompletedProcess(args, 255 if failure == 'github_ssh' else 1, '', 'successfully authenticated'))
    monkeypatch.setattr(onboard, 'signed_in_agent', lambda: None if failure == 'agent' else 'codex')
    clone = Mock()
    monkeypatch.setattr(onboard, 'ensure_repository_checkouts', clone)
    config = tmp_path / 'engine.toml'
    config.write_text('# preserve\n')
    registry = RepositoryRegistry(projects={})
    starters = AsyncMock()
    service = onboard.RepositoryOnboarding(registry, config, starters)

    async def run():
        state = service.start('owner/repo', str(tmp_path / 'repo'), 'connected')
        await service.close()
        return service.jobs[state['id']]

    state = asyncio.run(run())
    assert state['status'] == 'failed'
    assert [problem['code'] for problem in state['problems']] == [failure]
    assert state['problems'][0]['fix']
    clone.assert_not_called()
    starters.assert_not_called()
    assert not (tmp_path / 'repo').exists()
    assert config.read_text() == '# preserve\n'
    assert registry.snapshot.repos == {}


@pytest.mark.parametrize('existing', [True, False])
def test_job_reuses_checkout_persists_applies_and_is_idempotent(tmp_path, monkeypatch, existing):
    target = tmp_path / 'repo'
    if existing:
        target.mkdir()
        (target / 'local-changes').write_text('keep')
    monkeypatch.setattr(onboard, 'preflight', lambda: ([], 'codex'))
    monkeypatch.setattr(onboard, '_run', lambda args: subprocess.CompletedProcess(
        args, 0, str(target) if 'rev-parse' in args else 'git@github.com:owner/repo.git', '',
    ))
    clone = Mock(side_effect=lambda repos: target.mkdir(exist_ok=True))
    monkeypatch.setattr(onboard, 'ensure_repository_checkouts', clone)
    monkeypatch.setattr(RepositoryRegistry, '_project', lambda self, path: 'owner/repo')
    registry = RepositoryRegistry(projects={})
    starters = AsyncMock()
    config = tmp_path / 'explicit.toml'
    service = onboard.RepositoryOnboarding(registry, config, starters)

    async def run():
        first = service.start('owner/repo', str(target), 'disconnected')
        duplicate = service.start('owner/repo', str(target), 'disconnected')
        assert first['id'] == duplicate['id']
        await service.close()
        assert service.jobs[first['id']]['status'] == 'complete'
        stamp = config.stat().st_mtime_ns
        again = service.start('https://github.com/owner/repo.git', None, 'connected')
        assert again['message'] == 'Already onboarded'
        assert config.stat().st_mtime_ns == stamp

    asyncio.run(run())
    assert registry.snapshot.repos == {'owner/repo': str(target)}
    assert registry.snapshot.repo_modes == {'owner/repo': 'disconnected'}
    assert tomllib.loads(config.read_text())['repos'] == dict(registry.snapshot.repos)
    starters.assert_awaited_once_with('owner/repo', 'codex')
    clone.assert_called_once()
    if existing:
        assert (target / 'local-changes').read_text() == 'keep'


def test_wrong_checkout_is_never_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr(onboard, '_run', lambda args: subprocess.CompletedProcess(args, 0, 'https://github.com/other/repo', ''))
    with pytest.raises(onboard.OnboardingError, match='not a checkout'):
        onboard.validate_checkout('owner/repo', tmp_path)


def test_registers_the_default_graphs_for_the_signed_in_agent():
    service = Mock()
    service.add_graph = AsyncMock()
    asyncio.run(onboard.register_starters(service, 'owner/repo', 'opencode'))
    assert service.add_graph.await_count == 3
    for name in onboard.starters.NAMES:
        service.add_graph.assert_any_await(
            'owner/repo', source=onboard.starters.source(name, runner='opencode'), format='python',
        )


@pytest.mark.parametrize("origin", [None, "http://localhost", "http://127.0.0.1"])
def test_api_job_and_config_share_running_registry(tmp_path, monkeypatch, origin):
    from test_web_app import ConcurrentRunner, _session
    from engine.apps.web.api import create_app
    from engine.runtime import WorkflowCatalog

    target = tmp_path / 'repo'
    monkeypatch.setattr(onboard, 'preflight', lambda: ([], 'codex'))
    monkeypatch.setattr(onboard, 'validate_checkout', lambda *args: None)
    monkeypatch.setattr(onboard, 'ensure_repository_checkouts', lambda repos: target.mkdir())
    monkeypatch.setattr(onboard, 'register_starters', AsyncMock())
    monkeypatch.setattr(RepositoryRegistry, '_project', lambda self, path: 'owner/repo')
    registry = RepositoryRegistry(projects={})
    runner = ConcurrentRunner()
    app = create_app(_session(runner), {'test': runner}, repos=registry,
                     workflow_catalog=WorkflowCatalog.from_graphs(()), config_path=tmp_path / 'custom.toml')

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            response = await client.post('/api/repositories/onboard', json={'repository': 'owner/repo', 'path': str(target)},
                                         headers={'Origin': origin} if origin else {})
            assert response.status_code == 202
            job = response.json()
            for _ in range(100):
                result = (await client.get(f"/api/repositories/onboard/{job['id']}")).json()
                if result['status'] in {'complete', 'failed'}:
                    break
                await asyncio.sleep(.01)
            assert result['status'] == 'complete', result
            config = (await client.get('/api/config')).json()
            assert config['hasRepositories'] is True
            assert config['repositories'] == [{'name': 'owner/repo', 'path': str(target)}]

    asyncio.run(run())


@pytest.mark.parametrize('agent', ['claude', 'codex', 'opencode'])
def test_detects_each_signed_in_agent(agent, tmp_path, monkeypatch):
    monkeypatch.setattr(onboard.shutil, 'which', lambda binary: binary if binary == agent else None)
    monkeypatch.setattr(onboard, '_run', lambda args: subprocess.CompletedProcess(args, 0, '{"loggedIn": true}', ''))
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
    auth = tmp_path / 'opencode/auth.json'
    auth.parent.mkdir()
    auth.write_text('{"provider": {"type": "oauth", "refresh": "test-refresh"}}')
    assert onboard.signed_in_agent() == agent


def test_no_credentials_and_ssh_timeout_are_actionable(monkeypatch):
    monkeypatch.setattr(onboard.shutil, 'which', lambda binary: binary if binary == 'git' else None)
    monkeypatch.setattr(onboard, '_run', Mock(side_effect=subprocess.TimeoutExpired('ssh', 20)))
    problems, agent = onboard.preflight()
    assert agent is None
    assert {item['code'] for item in problems} == {'agent', 'github_ssh'}


def test_onboarding_is_operator_only(tmp_path):
    from starlette.applications import Starlette
    service = onboard.RepositoryOnboarding(RepositoryRegistry(projects={}), tmp_path / 'config.toml', AsyncMock())
    app = Starlette(routes=service.routes(AsyncMock(return_value={'/visible/repo'}), lambda request: True))

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            assert (await client.post('/api/repositories/onboard', json={'repository': 'owner/repo'})).status_code == 403
            assert (await client.get('/api/repositories/onboard/unknown')).status_code == 403
    asyncio.run(run())
    assert service.jobs == {}
    assert not service.config_path.exists()


def test_failed_job_can_be_retried_without_recloning(tmp_path, monkeypatch):
    target = tmp_path / 'repo'
    monkeypatch.setattr(onboard, 'preflight', lambda: ([], 'codex'))
    monkeypatch.setattr(onboard, 'validate_checkout', lambda *args: None)
    monkeypatch.setattr(onboard, 'ensure_repository_checkouts', lambda repos: target.mkdir(exist_ok=True))
    monkeypatch.setattr(RepositoryRegistry, '_project', lambda self, path: 'owner/repo')
    register = AsyncMock(side_effect=[RuntimeError('unavailable'), None])
    service = onboard.RepositoryOnboarding(RepositoryRegistry(projects={}), tmp_path / 'config.toml', register)

    async def run():
        first = service.start('owner/repo', str(target), 'connected')
        await service.close()
        assert service.jobs[first['id']]['status'] == 'failed'
        assert not service.config_path.exists()
        second = service.start('owner/repo', str(target), 'connected')
        await service.close()
        assert service.jobs[second['id']]['status'] == 'complete'
    asyncio.run(run())


def test_fresh_daemon_registers_starters_and_offers_them_without_restart(tmp_path, monkeypatch):
    from test_web_app import ConcurrentRunner, _session
    from engine.apps.web.api import create_app
    from engine.apps.web.composition import Settings, build_graph_runtime, build_graph_service
    from engine.runtime import WorkflowCatalog

    target = tmp_path / 'repo'
    monkeypatch.setattr(onboard, 'preflight', lambda: ([], 'codex'))
    monkeypatch.setattr(onboard, 'validate_checkout', lambda *args: None)
    monkeypatch.setattr(onboard, 'ensure_repository_checkouts', lambda repos: target.mkdir())
    monkeypatch.setattr(RepositoryRegistry, '_project', lambda self, path: 'owner/repo')
    registry = RepositoryRegistry(projects={})
    settings = Settings(graph_state_directory=str(tmp_path / 'graphs'), workspace_root=str(tmp_path / 'workspaces'))
    runner = ConcurrentRunner()
    app = create_app(_session(runner), {'test': runner}, repos=registry,
                     workflow_catalog=WorkflowCatalog.from_graphs(()), config_path=tmp_path / 'config.toml',
                     graph_runtime=build_graph_runtime(settings, ()),
                     graph_service=build_graph_service(settings, repositories=registry))

    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
                assert (await client.get('/api/config')).json()['workflows'] == []
                job = (await client.post('/api/repositories/onboard', json={'repository': 'owner/repo', 'path': str(target)})).json()
                for _ in range(200):
                    state = (await client.get(f"/api/repositories/onboard/{job['id']}")).json()
                    if state['status'] in {'complete', 'failed'}:
                        break
                    await asyncio.sleep(.01)
                assert state['status'] == 'complete', state
                config = (await client.get('/api/config')).json()
                assert len(config['workflows']) == 3
                graphs = (await client.get('/api/v1/graphs', params={'project': 'owner/repo'})).json()['graphs']
                assert {graph['name'] for graph in graphs} == set(onboard.starters.NAMES)
                assert config['repositories'] == [{'name': 'owner/repo', 'path': str(target)}]
    asyncio.run(run())


def test_default_checkout_is_under_daemon_home(tmp_path, monkeypatch):
    monkeypatch.setattr(onboard.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(onboard, 'preflight', lambda: ([{'code': 'agent', 'message': 'sign in', 'fix': 'engine agent signin codex'}], None))
    service = onboard.RepositoryOnboarding(RepositoryRegistry(projects={}), None, AsyncMock())

    async def run():
        job = service.start('https://github.com/owner/repo.git', None, 'connected')
        assert job['path'] == str(tmp_path / 'code/repo')
        await service.close()
    asyncio.run(run())
    assert not (tmp_path / 'code').exists()


def test_configured_onboarding_directory_is_used(tmp_path, monkeypatch):
    monkeypatch.setattr(onboard.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(onboard, 'preflight', lambda: ([{'code': 'agent', 'message': 'sign in', 'fix': 'x'}], None))
    directory = tmp_path / 'code' / 'work'
    service = onboard.RepositoryOnboarding(RepositoryRegistry(projects={}), None, AsyncMock(), onboarding_directory=directory)

    async def run():
        job = service.start('https://github.com/owner/repo.git', None, 'connected')
        assert job['path'] == str(directory / 'repo')
        await service.close()
    asyncio.run(run())
    assert not (directory / 'repo').exists()


def test_explicit_path_overrides_onboarding_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(onboard.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(onboard, 'preflight', lambda: ([{'code': 'agent', 'message': 'sign in', 'fix': 'x'}], None))
    service = onboard.RepositoryOnboarding(RepositoryRegistry(projects={}), None, AsyncMock(), onboarding_directory=tmp_path / 'code' / 'work')

    async def run():
        job = service.start('https://github.com/owner/repo.git', str(tmp_path / 'elsewhere'), 'connected')
        assert job['path'] == str(tmp_path / 'elsewhere')
        await service.close()
    asyncio.run(run())



@pytest.mark.parametrize("origin", ["https://evil.example", "http://localhost.evil.example", "null"])
def test_cross_origin_onboarding_never_starts(tmp_path, monkeypatch, origin):
    from test_web_app import ConcurrentRunner, _session
    from engine.apps.web.api import create_app
    from engine.runtime import WorkflowCatalog

    start = Mock()
    monkeypatch.setattr(onboard.RepositoryOnboarding, 'start_request', start)
    runner = ConcurrentRunner()
    app = create_app(_session(runner), {'test': runner}, repos=RepositoryRegistry(projects={}),
                     workflow_catalog=WorkflowCatalog.from_graphs(()), config_path=tmp_path / 'config.toml')

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            response = await client.post('/api/repositories/onboard',
                                         content='{"repository": "owner/repo"}',
                                         headers={'Origin': origin, 'Content-Type': 'text/plain'})
            assert response.status_code == 403
            assert response.json() == {'error': 'forbidden'}
    asyncio.run(run())
    start.assert_not_called()
    assert not (tmp_path / 'config.toml').exists()


@pytest.mark.parametrize("origin", ["https://evil.example", "http://localhost.evil.example", "null"])
@pytest.mark.parametrize("path", ['/api/v1/repositories/onboard', '/api/v1/repositories/onboard/job1'])
def test_cross_origin_v1_onboarding_never_starts(tmp_path, monkeypatch, origin, path):
    from test_web_app import ConcurrentRunner, _session
    from engine.apps.web.api import create_app
    from engine.runtime import WorkflowCatalog

    start = Mock()
    monkeypatch.setattr(onboard.RepositoryOnboarding, 'start_request', start)
    runner = ConcurrentRunner()
    app = create_app(_session(runner), {'test': runner}, repos=RepositoryRegistry(projects={}),
                     workflow_catalog=WorkflowCatalog.from_graphs(()), config_path=tmp_path / 'config.toml')

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            response = await client.request('POST' if path.endswith('onboard') else 'GET', path,
                                            content='{"repository": "owner/repo"}',
                                            headers={'Origin': origin, 'Content-Type': 'text/plain'})
            assert response.status_code == 403
            assert response.json() == {'error': 'forbidden'}
    asyncio.run(run())
    start.assert_not_called()
    assert not (tmp_path / 'config.toml').exists()


@pytest.mark.parametrize('origin', [None, 'http://evil.example', 'http://localhost'])
@pytest.mark.parametrize('method,path', [
    ('POST', '/api/repositories/onboard'),
    ('GET', '/api/repositories/onboard/job1'),
    ('GET', '/api/config'),
    ('POST', '/api/github/connect'),
])
def test_rebound_host_cannot_reach_daemon(tmp_path, monkeypatch, origin, method, path):
    from test_web_app import ConcurrentRunner, _session
    from engine.apps.web.api import create_app
    from engine.runtime import WorkflowCatalog

    start = Mock()
    monkeypatch.setattr(onboard.RepositoryOnboarding, 'start_request', start)
    runner = ConcurrentRunner()
    app = create_app(_session(runner), {'test': runner}, repos=RepositoryRegistry(projects={}),
                     workflow_catalog=WorkflowCatalog.from_graphs(()), config_path=tmp_path / 'config.toml')

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://evil.example') as client:
            headers = {'Origin': origin} if origin else {}
            headers['Content-Type'] = 'text/plain'
            response = await client.request(method, path, headers=headers, content='{"repository":"owner/repo"}')
            assert response.status_code == 403
    asyncio.run(run())
    start.assert_not_called()
    assert not (tmp_path / 'config.toml').exists()


@pytest.mark.parametrize('base_url,public_url', [
    ('http://localhost:4364', ''),
    ('http://127.0.0.1:4364', ''),
    ('http://[::1]:4364', ''),
    ('https://engine.example:8443', 'https://engine.example:8443'),
])
def test_trusted_hosts_can_onboard(tmp_path, monkeypatch, base_url, public_url):
    from test_web_app import ConcurrentRunner, _session
    from engine.apps.web.api import create_app
    from engine.runtime import WorkflowCatalog

    start = Mock(return_value={'id': 'job1', 'status': 'queued'})
    monkeypatch.setattr(onboard.RepositoryOnboarding, 'start_request', start)
    runner = ConcurrentRunner()
    app = create_app(_session(runner), {'test': runner}, repos=RepositoryRegistry(projects={}),
                     workflow_catalog=WorkflowCatalog.from_graphs(()), config_path=tmp_path / 'config.toml',
                     public_url=public_url)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url) as client:
            assert (await client.get('/api/config')).status_code == 200
            response = await client.post('/api/repositories/onboard', headers={'Origin': base_url},
                                         json={'repository': 'owner/repo'})
            assert response.status_code == 202
    asyncio.run(run())
    start.assert_called_once()
