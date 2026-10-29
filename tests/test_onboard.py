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
    'git@evil.example:owner/repo.git',
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
    assert tomllib.loads(first)['repos']['owner/repo'] == {'path': str(tmp_path / 'repo'), 'mode': 'disconnected'}
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
    assert tomllib.loads(service.config_path.read_text())['repos']['owner/repo']['path'] == str(tmp_path / 'repo')


@pytest.mark.parametrize('failure', ['git', 'agent'])
def test_preflight_failures_leave_destination_and_config_untouched(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(onboard.shutil, 'which', lambda binary: None if failure == 'git' and binary == 'git' else binary)
    monkeypatch.setattr(onboard, '_run', lambda args: subprocess.CompletedProcess(args, 1, '', 'successfully authenticated'))
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
@pytest.mark.parametrize('trusted', [None, True, False])
def test_job_reuses_checkout_persists_applies_and_is_idempotent(tmp_path, monkeypatch, existing, trusted):
    target = tmp_path / 'repo'
    if existing:
        target.mkdir()
        (target / 'local-changes').write_text('keep')
    monkeypatch.setattr(onboard, 'preflight', lambda: ([], 'codex'))
    monkeypatch.setattr(onboard, '_run', lambda args: subprocess.CompletedProcess(
        args, 0, str(target) if 'rev-parse' in args else 'git@github.com:owner/repo.git', '',
    ))
    clone = Mock(side_effect=lambda repos, **kwargs: target.mkdir(exist_ok=True))
    monkeypatch.setattr(onboard, 'ensure_repository_checkouts', clone)
    monkeypatch.setattr(RepositoryRegistry, '_project', lambda self, path: 'owner/repo')
    registry = RepositoryRegistry(projects={})
    starters = AsyncMock()
    config = tmp_path / 'explicit.toml'
    service = onboard.RepositoryOnboarding(registry, config, starters)

    async def run():
        first = service.start('owner/repo', str(target), 'disconnected', trusted)
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
    assert registry.lookup('owner/repo')[1] is trusted
    assert onboard.parse_engine_config(tomllib.loads(config.read_text())).repos == dict(registry.snapshot.repos)
    starters.assert_awaited_once_with('owner/repo', 'codex')
    clone.assert_called_once()
    if existing:
        assert (target / 'local-changes').read_text() == 'keep'


def test_wrong_checkout_is_never_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr(onboard, '_run', lambda args: subprocess.CompletedProcess(args, 0, 'https://github.com/other/repo', ''))
    with pytest.raises(onboard.OnboardingError, match='not a checkout'):
        onboard.validate_checkout('owner/repo', tmp_path)


@pytest.mark.parametrize('runner', ['claude', 'opencode', 'codex'])
def test_registers_starters_with_detected_runner_and_default_graph_policies(runner):
    service = Mock()
    service.add_graph = AsyncMock()
    asyncio.run(onboard.register_starters(service, 'owner/repo', runner))
    assert service.add_graph.await_count == 3
    for name, call in zip(onboard.starters.NAMES, service.add_graph.await_args_list):
        assert call.args == ('owner/repo',)
        assert call.kwargs['format'] == 'python'
        source = call.kwargs['source']
        if name == onboard.starters.DEFAULT:
            assert 'DEFAULT_RUNNER = "round-robin"' in source
            assert 'DEFAULT_REVIEW_RUNNER = "round-robin"' in source
        else:
            assert f'DEFAULT_RUNNER = {runner!r}' in source


@pytest.mark.parametrize("origin", [None, "http://localhost", "http://127.0.0.1"])
def test_api_job_and_config_share_running_registry(tmp_path, monkeypatch, origin):
    from test_web_app import ConcurrentRunner, _session
    from engine.apps.web.api import create_app
    from engine.runtime import WorkflowCatalog

    target = tmp_path / 'repo'
    monkeypatch.setattr(onboard, 'preflight', lambda: ([], 'codex'))
    monkeypatch.setattr(onboard, 'validate_checkout', lambda *args: None)
    monkeypatch.setattr(onboard, 'ensure_repository_checkouts', lambda repos, **kwargs: target.mkdir())
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
            assert config['repositories'] == [{'name': 'owner/repo', 'path': str(target), 'trusted': None}]

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


def test_no_credentials_are_actionable_without_ssh_probe(monkeypatch):
    monkeypatch.setattr(onboard.shutil, 'which', lambda binary: binary if binary == 'git' else None)
    monkeypatch.setattr(onboard, '_run', Mock(side_effect=subprocess.TimeoutExpired('ssh', 20)))
    problems, agent = onboard.preflight()
    assert agent is None
    assert {item['code'] for item in problems} == {'agent'}
    onboard._run.assert_not_called()


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
    monkeypatch.setattr(onboard, 'ensure_repository_checkouts', lambda repos, **kwargs: target.mkdir(exist_ok=True))
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
    monkeypatch.setattr(onboard, 'ensure_repository_checkouts', lambda repos, **kwargs: target.mkdir())
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
                assert config['repositories'] == [{'name': 'owner/repo', 'path': str(target), 'trusted': None}]
                from engine.apps.web.github_login import GitHubLogin
                monkeypatch.setattr(GitHubLogin, 'visible_repositories', AsyncMock(return_value={'owner/repo'}))
                for method in ('GET', 'POST'):
                    response = await client.request(method, '/api/v1/repositories/trust',
                        params={'repository': 'owner/repo'}, json={'repository': 'owner/repo', 'trusted': True})
                    assert response.status_code == 403
                assert registry.lookup('owner/repo')[1] is None

    asyncio.run(run())


def test_default_checkout_is_under_daemon_home(tmp_path, monkeypatch):
    monkeypatch.setattr(onboard.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(onboard, 'preflight', lambda: ([{'code': 'agent', 'message': 'sign in', 'fix': 'oe agent signin codex'}], None))
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


@pytest.mark.parametrize('origin', [None, 'http://evil.example', 'http://localhost'])
@pytest.mark.parametrize('method,path', [
    ('POST', '/api/repositories/onboard'),
    ('GET', '/api/repositories/onboard/job1'),
    ('GET', '/api/v1/repositories/trust?repository=owner/repo'),
    ('POST', '/api/v1/repositories/trust'),
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
def test_trusted_hosts_allow_local_onboarding_but_public_requires_auth(tmp_path, monkeypatch, base_url, public_url):
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
            if public_url:
                # A trusted public Host is not an authentication credential.
                assert (await client.get('/api/config')).status_code == 401
                response = await client.post('/api/repositories/onboard', headers={'Origin': base_url},
                                             json={'repository': 'owner/repo'})
                assert response.status_code == 401
                return
            assert (await client.get('/api/config')).status_code == 200
            response = await client.post('/api/repositories/onboard', headers={'Origin': base_url},
                                         json={'repository': 'owner/repo'})
            assert response.status_code == 202
    asyncio.run(run())
    if public_url:
        start.assert_not_called()
    else:
        start.assert_called_once()


@pytest.mark.parametrize("kind", ["claude", "codex", "opencode"])
def test_runner_must_be_installed(kind, monkeypatch):
    monkeypatch.setattr(onboard.shutil, "which", lambda _: None)
    assert not onboard.agent_available(kind, url="http://localhost:1234")


@pytest.mark.parametrize("result", [
    subprocess.CompletedProcess([], 1, "", "not signed in"),
    subprocess.CompletedProcess([], 0, '{"loggedIn": false}', ""),
    subprocess.CompletedProcess([], 0, 'invalid json', ""),
])
def test_claude_must_be_signed_in(result, monkeypatch):
    monkeypatch.setattr(onboard.shutil, "which", lambda name: name)
    monkeypatch.setattr(onboard, "_run", lambda _: result)
    assert not onboard.agent_available("claude")


def test_opencode_checks_the_added_agents_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(onboard.shutil, "which", lambda name: name)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    auth = tmp_path / "opencode/auth.json"
    auth.parent.mkdir()
    auth.write_text('{"qwen": {"type": "oauth", "refresh": "test"}}')
    assert onboard.agent_available("opencode", model="qwen/coder")
    assert not onboard.agent_available("opencode", model="other/coder")
    auth.unlink()
    assert not onboard.agent_available("opencode")
    assert onboard.agent_available("opencode", url="http://localhost:1234")


@pytest.mark.parametrize('reference', [
    'https://gitlab.com/group/subgroup/repo.git',
    'git@gitlab.com:group/subgroup/repo.git',
    'ssh://git@gitlab.com/group/subgroup/repo.git',
])
def test_gitlab_repository_references(reference):
    assert onboard.repository_name(reference) == 'gitlab.com/group/subgroup/repo'


@pytest.mark.parametrize('reference', ['https://github.com/owner/repo.git', 'https://gitlab.com/group/sub/repo.git'])
def test_clone_preserves_remote_url(tmp_path, monkeypatch, reference):
    from engine.apps.web import repositories
    name = onboard.repository_name(reference)
    target = tmp_path / 'repo'
    commands = []
    def clone(args, **kwargs):
        commands.append(args)
        from pathlib import Path
        Path(args[-1]).mkdir()
        return subprocess.CompletedProcess(args, 0, '', '')
    monkeypatch.setattr(repositories.subprocess, 'run', clone)
    repositories.ensure_repository_checkouts({name: str(target)}, clone_urls={name: reference})
    assert commands[0][0:4] == ['git', 'clone', '--', reference]
    assert target.is_dir()
    repositories.ensure_repository_checkouts({name: str(target)}, clone_urls={name: reference})
    assert len(commands) == 1


@pytest.mark.parametrize('local', [True, False])
def test_add_gitlab_local_or_remote(tmp_path, monkeypatch, local):
    target = tmp_path / 'repo'
    if local:
        target.mkdir()
    origin = 'https://gitlab.com/group/sub/repo.git'
    monkeypatch.setattr(onboard, '_run', lambda args: subprocess.CompletedProcess(
        args, 0, str(target) if 'rev-parse' in args else origin, ''))
    monkeypatch.setattr(onboard, 'preflight', lambda: ([], 'codex'))
    monkeypatch.setattr(RepositoryRegistry, '_project', lambda *args: None)
    clone = Mock(side_effect=lambda repos, **kwargs: target.mkdir(exist_ok=True))
    monkeypatch.setattr(onboard, 'ensure_repository_checkouts', clone)
    config = tmp_path / 'engine.toml'
    service = onboard.RepositoryOnboarding(RepositoryRegistry(projects={}), config, AsyncMock(), tmp_path)
    async def run():
        job = service.start(str(target) if local else origin, None, 'connected')
        await service.close()
        assert service.jobs[job['id']]['status'] == 'complete'
    asyncio.run(run())
    assert tomllib.loads(config.read_text())['repos'] == {'gitlab.com/group/sub/repo': {'path': str(target)}}
    assert clone.call_args.kwargs['clone_urls'] == {'gitlab.com/group/sub/repo': str(target) if local else origin}


@pytest.mark.parametrize('origin', ['', 'https://bitbucket.org/team/demo.git', '/srv/repos/demo.git', 'team/demo'])
@pytest.mark.parametrize('trust_first', [True, False])
def test_local_only_onboarding_and_trust(tmp_path, monkeypatch, origin, trust_first):
    target = tmp_path / 'demo'
    target.mkdir()
    monkeypatch.setattr(onboard, '_run', lambda args: subprocess.CompletedProcess(
        args, 0 if 'rev-parse' in args or origin else 2,
        str(target) if 'rev-parse' in args else origin, '',
    ))
    monkeypatch.setattr(onboard, 'preflight', lambda: ([], 'codex'))
    monkeypatch.setattr(RepositoryRegistry, '_project', lambda self, path: None)
    clone = Mock()
    monkeypatch.setattr(onboard, 'ensure_repository_checkouts', clone)
    config = tmp_path / 'engine.toml'
    registry = RepositoryRegistry(projects={})
    service = onboard.RepositoryOnboarding(registry, config, AsyncMock())
    with pytest.raises(onboard.OnboardingError, match='Connected mode requires') as error:
        service.start(str(target), None, 'connected')
    assert 'or use disconnected mode' in error.value.problem['fix']

    if trust_first:
        # The first trust answer works before onboarding and persists all policy.
        assert service.repository_trust(str(target))['trusted'] is None
        assert service.repository_trust(str(target), True)['trusted'] is True
        assert tomllib.loads(config.read_text()) == {
            'repos': {'demo': {'path': str(target), 'mode': 'disconnected', 'trusted': True}},
        }
        assert service.repository_trust(str(target))['trusted'] is True

    async def run():
        job = service.start_request({'repository': str(target)})
        await service.close()
        assert service.jobs[job['id']]['status'] == 'complete'
        assert job['mode'] == 'disconnected'
        assert job['path'] == str(target)
    asyncio.run(run())
    clone.assert_not_called()
    assert registry.snapshot.disconnected == frozenset({target})
    saved = tomllib.loads(config.read_text())
    assert saved["repos"]["demo"]["path"] == str(target)
    assert saved["repos"]["demo"]["mode"] == "disconnected"


def test_trust_reserves_active_onboarding_names(tmp_path, monkeypatch):
    first, second = tmp_path / 'one/demo', tmp_path / 'two/demo'
    for checkout in (first, second):
        checkout.mkdir(parents=True)
    monkeypatch.setattr(onboard, '_run', lambda args: subprocess.CompletedProcess(
        args, 0 if 'rev-parse' in args else 2,
        args[2] if 'rev-parse' in args else '', '',
    ))
    monkeypatch.setattr(onboard, 'preflight', lambda: ([], 'codex'))
    monkeypatch.setattr(RepositoryRegistry, '_project', lambda self, path: None)
    config = tmp_path / 'engine.toml'
    registry = RepositoryRegistry(projects={})
    service = onboard.RepositoryOnboarding(registry, config, AsyncMock())

    async def run():
        job = service.start(str(first), None)
        try:
            assert job['repository'] == 'demo'
            assert service.jobs[job['id']]['status'] == 'queued'
            assert not config.exists()
            assert service.repository_trust(str(first))['name'] == 'demo'
            for action in (
                lambda: service.repository_trust(str(second)),
                lambda: service.repository_trust(str(second), True),
                lambda: service.start(str(second), None),
            ):
                with pytest.raises(onboard.OnboardingError, match="conflicts with") as error:
                    action()
                assert str(first) in str(error.value)
                assert str(second) in str(error.value)
                assert "alias" in error.value.problem["fix"]
                assert "rename" in error.value.problem["fix"]
            assert not config.exists()
        finally:
            await service.close()
        assert service.jobs[job['id']]['status'] == 'complete'

    asyncio.run(run())
    saved = tomllib.loads(config.read_text())
    assert onboard.parse_engine_config(saved).repos == registry.snapshot.repos == {'demo': str(first)}
    assert saved['repos']['demo']['mode'] == 'disconnected'
    assert 'trusted_repos' not in saved
    with pytest.raises(onboard.OnboardingError, match="conflicts with"):
        service.repository_trust(str(second), True)


def test_local_names_are_sanitized_and_collisions_require_an_alias(tmp_path, monkeypatch):
    monkeypatch.setattr(onboard, '_run', lambda args: subprocess.CompletedProcess(args, 2, '', ''))
    first, second = tmp_path / 'one/My demo!', tmp_path / 'two/My demo!'
    name, project = onboard.local_repository(first, {})
    assert (name, project) == ('My-demo', None)
    repos = {name: str(first)}
    with pytest.raises(onboard.OnboardingError, match="conflicts with"):
        onboard.local_repository(second, repos)
    repos['my-alias'] = str(second)
    assert onboard.local_repository(second, repos) == ('my-alias', None)
    assert onboard.local_repository(second.with_name('renamed'), repos) == ('renamed', None)
    assert onboard.local_repository(first, repos) == (name, None)
    assert onboard.local_repository(tmp_path / '!!!', {}) == ('repo', None)
    onboard.parse_engine_config({'repos': repos})


@pytest.mark.parametrize('origin,expected', [
    ('git@github.com:owner/repo.git', 'owner/repo'),
    ('https://gitlab.com/group/sub/repo.git', 'gitlab.com/group/sub/repo'),
])
def test_local_forge_names_are_preserved(tmp_path, monkeypatch, origin, expected):
    monkeypatch.setattr(onboard, '_run', lambda args: subprocess.CompletedProcess(args, 0, origin, ''))
    assert onboard.local_repository(tmp_path, {}) == (expected, expected)


def test_persist_rejects_name_collision_with_actionable_error(tmp_path):
    config = tmp_path / 'engine.toml'
    first, second = tmp_path / 'one/demo', tmp_path / 'two/demo'
    onboard.persist_repository(config, 'demo', str(first), 'disconnected', True)
    original = config.read_text()
    # A stale allocation from a concurrent request must also surface the error.
    with pytest.raises(onboard.OnboardingError, match="conflicts with") as error:
        onboard.persist_repository(config, 'demo', str(second), 'disconnected', False)
    assert "alias" in error.value.problem["fix"]
    assert "rename" in error.value.problem["fix"]
    assert config.read_text() == original


@pytest.mark.parametrize('method', ['GET', 'POST'])
def test_trust_api_surfaces_collision_and_recovery(tmp_path, monkeypatch, method):
    from engine.graph_service.api import create_app

    first, second = tmp_path / 'one/demo', tmp_path / 'two/demo'
    monkeypatch.setattr(onboard, '_run', lambda args: subprocess.CompletedProcess(
        args, 0 if 'rev-parse' in args else 2,
        args[2] if 'rev-parse' in args else '', '',
    ))
    registry = RepositoryRegistry({'demo': str(first)}, projects={})
    onboarding = onboard.RepositoryOnboarding(registry, tmp_path / 'engine.toml', AsyncMock())
    app = create_app(Mock(), repository_trust=onboarding.repository_trust)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            response = await client.request(method, '/repositories/trust',
                params={'repository': str(second)}, json={'repository': str(second), 'trusted': True})
            assert response.status_code == 400
            message = response.json()['error']
            assert "conflicts with" in message
            assert str(first) in message and str(second) in message
            assert "alias" in message and "rename" in message

    asyncio.run(run())
    assert not onboarding.config_path.exists()


def test_update_nested_repository_preserves_other_settings(tmp_path):
    config = tmp_path / 'engine.toml'
    config.write_text('''[repos."owner/repo"]
path = "/repo"
mode = "disconnected"
trusted = false
webhooks = false
default = true
auto_approve_green = true
''')
    onboard.persist_repository(config, 'owner/repo', '/repo', 'connected', True)
    assert tomllib.loads(config.read_text()) == {'repos': {'owner/repo': {
        'path': '/repo', 'mode': 'disconnected', 'trusted': True,
        'webhooks': False, 'default': True, 'auto_approve_green': True,
    }}}


def test_update_legacy_repository_moves_only_its_policy(tmp_path):
    config = tmp_path / 'engine.toml'
    config.write_text('''[repos]
"owner/repo" = "/repo"
other = "/other" # preserve
[repo_modes]
"owner/repo" = "disconnected"
other = "connected"
[trusted_repos]
"owner/repo" = false
other = true
''')
    onboard.persist_repository(config, 'owner/repo', '/repo', 'connected', True)
    saved = tomllib.loads(config.read_text())
    assert saved['repos']['owner/repo'] == {'path': '/repo', 'mode': 'disconnected', 'trusted': True}
    assert saved['repos']['other'] == '/other'
    assert saved['repo_modes'] == {'other': 'connected'}
    assert saved['trusted_repos'] == {'other': True}
    assert '# preserve' in config.read_text()


@pytest.mark.parametrize("github_login", [True, False])
def test_onboarding_credential_can_clone_and_trust_but_service_cannot(tmp_path, monkeypatch, github_login):
    from test_web_app import ConcurrentRunner, _session
    from engine.apps.web.api import create_app
    from engine.apps.web.github_login import GitHubLoginConfig
    from engine.runtime import WorkflowCatalog

    target = tmp_path / 'repo'
    config = tmp_path / 'engine.toml'
    clone = Mock(side_effect=lambda repos, **kwargs: target.mkdir())
    monkeypatch.setattr(onboard, 'preflight', lambda: ([], 'codex'))
    monkeypatch.setattr(onboard, 'validate_checkout', lambda *args: None)
    monkeypatch.setattr(onboard, 'ensure_repository_checkouts', clone)
    monkeypatch.setattr(onboard, 'register_starters', AsyncMock())
    monkeypatch.setattr(RepositoryRegistry, '_project', lambda self, path: 'owner/repo')
    runner = ConcurrentRunner()
    app = create_app(_session(runner), {'test': runner}, repos=RepositoryRegistry(projects={}),
                     workflow_catalog=WorkflowCatalog.from_graphs(()), config_path=config,
                     github_login_config=(GitHubLoginConfig('client', 'secret', 'http://localhost/api/auth/github/callback')
                                          if github_login else None),
                     public_url='https://engine.example',
                     service_token=lambda: 's' * 40, onboarding_token=lambda: 'o' * 40)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://localhost') as client:
            body = {'repository': 'https://github.com/owner/repo', 'path': str(target), 'trusted': True}
            response = await client.post('/api/repositories/onboard', json=body, headers={'Authorization': 'Bearer ' + 's' * 40})
            assert response.status_code == 401
            clone.assert_not_called()
            assert not config.exists()
            headers = {'Authorization': 'Bearer ' + 'o' * 40}
            response = await client.post('/api/repositories/onboard', json=body, headers=headers)
            assert response.status_code == 202
            assert 'secret' not in response.text
            for _ in range(100):
                response = await client.get('/api/repositories/onboard/' + response.json()['id'], headers=headers)
                assert 'secret' not in response.text
                if response.json()['status'] in {'complete', 'failed'}:
                    break
                await asyncio.sleep(.01)
            assert response.json()['status'] == 'complete', response.text
            assert 'trusted = true' in config.read_text()
            clone.assert_called_once()
            assert (await client.get('/api/config', headers=headers)).status_code == 401
    asyncio.run(run())
