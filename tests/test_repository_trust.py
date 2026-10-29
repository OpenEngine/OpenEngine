import argparse
import asyncio
import json
import tomllib
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from engine.apps.web.onboarding import RepositoryOnboarding, persist_repository
from engine.cli.commands import _check_trust
from engine.graph_service.api import create_app
from engine.runtime.config import EngineConfigError, parse_engine_config
from engine.runtime.repositories import RepositoryRegistry


def test_registry_decisions_are_atomic(tmp_path):
    registry = RepositoryRegistry({'owner/repo': str(tmp_path)}, projects={})
    original = registry.snapshot
    assert registry.lookup(str(tmp_path)) == ('owner/repo', None)
    registry.trust('owner/repo', True)
    assert registry.snapshot.trusted == frozenset({tmp_path})
    assert not original.trusted
    registry.trust('owner/repo', False)
    assert registry.lookup(str(tmp_path)) == ('owner/repo', False)
    assert not registry.snapshot.trusted
    with pytest.raises(EngineConfigError):
        registry.trust('missing', True)


@pytest.mark.parametrize('trusted', [True, False])
@pytest.mark.parametrize('existing', [True, False])
def test_persist_decision(tmp_path, trusted, existing):
    path = tmp_path / 'engine.toml'
    path.write_text('# keep me\n' + (f'[repos]\n"owner/repo" = "{tmp_path}" # checkout\n' if existing else ''))
    persist_repository(path, 'owner/repo', str(tmp_path), 'connected', trusted)
    assert '# keep me' in path.read_text()
    if existing:
        assert '# checkout' in path.read_text()
    config = parse_engine_config(tomllib.loads(path.read_text()))
    assert config.repository_trust == {'owner/repo': trusted}
    assert ('owner/repo' in config.trusted_repos) is trusted
    assert RepositoryRegistry(config.repos, trusted_repos=config.repository_trust, projects={}).lookup(str(tmp_path))[1] is trusted


@pytest.mark.parametrize('tty,json_mode,flag,answer,expected', [
    (True, False, None, 'y', True), (True, False, None, 'n', False),
    (False, False, None, '', None), (True, True, None, '', None),
    (False, True, True, '', True), (False, False, False, '', False),
])
def test_cli_decision(monkeypatch, tty, json_mode, flag, answer, expected):
    client = Mock()
    client.get.side_effect = lambda path, **kwargs: {'autoApprove': False} if path == '/backend' else {'name': 'owner/repo', 'repository': '/repo', 'trusted': None}
    monkeypatch.setattr('sys.stdin.isatty', lambda: tty)
    monkeypatch.setattr('sys.stdout.isatty', lambda: tty)
    prompt = Mock(return_value=answer)
    monkeypatch.setattr('builtins.input', prompt)
    _check_trust(argparse.Namespace(trust=flag, json=json_mode), client, '/repo')
    if expected is None:
        client.post.assert_not_called()
    else:
        client.post.assert_called_once_with('/repositories/trust', {'repository': '/repo', 'trusted': expected})
    assert prompt.called is (tty and not json_mode and flag is None)


def test_cli_skips_global_and_remembered():
    client = Mock()
    client.get.return_value = {'autoApprove': True}
    _check_trust(argparse.Namespace(), client, '/repo')
    client.get.assert_called_once_with('/backend')
    client.post.assert_not_called()
    client.get.side_effect = [{'autoApprove': False}, {'trusted': False}]
    _check_trust(argparse.Namespace(), client, '/repo')
    client.post.assert_not_called()


def test_api_trust_updates_live_registry(tmp_path):
    registry = RepositoryRegistry({'owner/repo': str(tmp_path)}, projects={})
    onboarding = RepositoryOnboarding(registry, tmp_path / 'engine.toml', AsyncMock())
    app = create_app(Mock(), repository_trust=onboarding.repository_trust)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
            assert (await client.get('/repositories/trust', params={'repository': 'owner/repo'})).json()['trusted'] is None
            for decision in [True, False]:
                response = await client.post('/repositories/trust', json={'repository': 'owner/repo', 'trusted': decision})
                assert response.status_code == 200
                assert response.json()['trusted'] is decision
                assert registry.lookup('owner/repo')[1] is decision
            assert (await client.post('/repositories/trust', json={'repository': 'owner/repo', 'trusted': 'yes'})).status_code == 400
    asyncio.run(run())


def test_trust_preserves_home_relative_config_path(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    config_path = tmp_path / 'engine.toml'
    original = '[repos]\n"owner/repo" = "~/checkout" # keep this path\n'
    config_path.write_text(original)
    config = parse_engine_config(tomllib.loads(original))
    registry = RepositoryRegistry(config.repos, projects={})
    onboarding = RepositoryOnboarding(registry, config_path, AsyncMock())
    for decision in (True, False):
        assert onboarding.repository_trust('owner/repo', decision)['trusted'] is decision
        assert registry.lookup(str(tmp_path / 'checkout')) == ('owner/repo', decision)
        assert 'path = "~/checkout" # keep this path' in config_path.read_text()
        saved = parse_engine_config(tomllib.loads(config_path.read_text()))
        assert saved.repository_trust == {'owner/repo': decision}


@pytest.mark.parametrize("login_configured", [False, True])
def test_trust_api_checks_origin_before_mutating_config(tmp_path, login_configured):
    from test_web_app import ConcurrentRunner, _session
    from engine.apps.web.api import create_app as create_web_app
    from engine.apps.web.composition import Settings, build_graph_runtime, build_graph_service
    from engine.runtime import WorkflowCatalog

    config_path = tmp_path / 'engine.toml'
    registry = RepositoryRegistry({'owner/repo': str(tmp_path / 'checkout')}, projects={})
    settings = Settings(graph_state_directory=str(tmp_path / 'graphs'), workspace_root=str(tmp_path / 'workspaces'))
    runner = ConcurrentRunner()
    from engine.apps.web.github_login import GitHubLoginConfig
    login = GitHubLoginConfig("client", "secret", "https://engine.example/api/auth/github/callback") if login_configured else None
    app = create_web_app(_session(runner), {'test': runner}, repos=registry,
                         workflow_catalog=WorkflowCatalog.from_graphs(()), config_path=config_path,
                         public_url='https://engine.example', github_login_config=login,
                         service_token=lambda: 't' * 40,
                         graph_runtime=build_graph_runtime(settings, ()),
                         graph_service=build_graph_service(settings, repositories=registry))

    async def run():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://localhost:4364') as client:
                # Enumerate the actual route tables, including the forwarded APIs.
                from engine.graph_runtime.api import create_app as runtime_app
                from engine.apps.web.request_security import _ORIGIN_EXEMPT
                tables = [("", app.routes), ("/api/v1", create_app(Mock()).routes),
                          ("/graph", runtime_app(Mock()).routes)]
                for prefix, routes in tables:
                    for route in routes:
                        path = prefix + route.path
                        if path in _ORIGIN_EXEMPT:
                            continue
                        for method in set(getattr(route, "methods", ()) or ()) & {"POST", "PUT", "PATCH", "DELETE"}:
                            response = await client.request(method, path, content='{}',
                                headers={"Origin": "https://evil.example", "Content-Type": "text/plain"})
                            assert response.status_code == 403, (method, path, response.text)
                marker = tmp_path / "imported"
                response = await client.post('/api/v1/graphs',
                    content=json.dumps({"project": "default", "format": "python", "name": "probe",
                        "source": f"from pathlib import Path; Path({str(marker)!r}).touch()"}),
                    headers={"Origin": "https://evil.example", "Content-Type": "text/plain"})
                assert response.status_code == 403
                assert not marker.exists()
                assert not list((tmp_path / 'graphs').rglob('*.py'))
                # All graph API writes reject a foreign Origin before parsing.
                for route in ('runs', 'loops', 'agents', 'sessions'):
                    response = await client.post(f'/api/v1/{route}', content='invalid JSON',
                        headers={'Origin': 'https://proxy.example', 'Content-Type': 'application/json'})
                    assert response.status_code == 403
                    assert response.json()['error'] == 'forbidden'
                for origin in ('https://evil.example', 'http://localhost.evil.example', 'null', 'http://localhost:9999'):
                    response = await client.post('/api/v1/repositories/trust',
                        content='{"repository":"owner/repo","trusted":true}',
                        headers={'Origin': origin, 'Content-Type': 'text/plain'})
                    assert response.status_code == 403
                    assert registry.lookup('owner/repo')[1] is None
                    assert not config_path.exists()
                if login_configured:
                    return  # Allowed origins still require a session in login mode.
                signed_in = await client.post('/api/auth/token/login', data={'token': 't' * 40},
                                              headers={'Origin': 'https://engine.example'})
                assert signed_in.status_code == 303
                client.headers['Cookie'] = f"engine_token_session={signed_in.cookies['engine_token_session']}"
                # An allowed Origin cannot grant a token session the trust-write
                # privilege that its bearer credential is explicitly denied.
                for origin in (None, 'http://localhost:4364', 'https://engine.example'):
                    response = await client.post('/api/v1/repositories/trust',
                        json={'repository': 'owner/repo', 'trusted': True},
                        headers={'Origin': origin} if origin else {})
                    assert response.status_code == 401
                    assert registry.lookup('owner/repo')[1] is None
                    assert not config_path.exists()
    asyncio.run(run())


def test_lookup_uses_git_identity(tmp_path, monkeypatch):
    import subprocess
    import engine.runtime.repositories as repositories
    root, folder, worktree = (tmp_path / name for name in ('root', 'folder', 'worktree'))
    identity = tmp_path / '.git'
    calls = []
    def git(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, str(identity), '')
    monkeypatch.setattr(repositories.subprocess, 'run', git)
    registry = RepositoryRegistry({'owner/repo': str(root)}, trusted_repos={'owner/repo': False}, projects={})
    assert registry.lookup(str(folder)) == ('owner/repo', False)
    assert registry.lookup(str(worktree)) == ('owner/repo', False)
    assert all(call[-1] == '--git-common-dir' for call in calls)


def test_concurrent_config_writes_preserve_decisions(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = tmp_path / 'engine.toml'
    path.write_text('# preserved\n')
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(persist_repository, path, f'owner/repo{i}', str(tmp_path / str(i)), 'connected', bool(i % 2)) for i in range(8)]
        for future in futures:
            future.result()
    assert parse_engine_config(tomllib.loads(path.read_text())).repository_trust == {f'owner/repo{i}': bool(i % 2) for i in range(8)}
    assert path.read_text().startswith('# preserved')


def test_first_checkout_registration(tmp_path, monkeypatch):
    import subprocess
    from engine.apps.web import onboarding as module
    root = tmp_path / 'checkout'
    root.mkdir()
    def git(args):
        return subprocess.CompletedProcess(args, 0, str(root) if '--show-toplevel' in args else 'git@github.com:owner/repo.git', '')
    monkeypatch.setattr(module, '_run', git)
    monkeypatch.setattr(RepositoryRegistry, '_project', lambda *args: 'owner/repo')
    registry = RepositoryRegistry(projects={})
    config = tmp_path / 'engine.toml'
    onboarding = RepositoryOnboarding(registry, config, AsyncMock())
    assert onboarding.repository_trust(str(root)) == {'repository': str(root), 'name': 'owner/repo', 'trusted': None}
    assert not config.exists()
    assert onboarding.repository_trust(str(root), True)['trusted'] is True
    assert registry.snapshot.trusted == frozenset({root})
    assert tomllib.loads(config.read_text())['repos'] == {'owner/repo': {'path': str(root), 'trusted': True}}


@pytest.mark.parametrize('command', [['go', 'change'], ['graph', 'run', 'default'], ['loop', 'add', 'default'], ['agent', 'codex'], ['repo', 'add', 'owner/repo']])
@pytest.mark.parametrize('flag,decision', [('--trust', True), ('--no-trust', False)])
def test_trust_flags(command, flag, decision):
    from engine.apps.cli.__main__ import parser
    assert parser().parse_args([*command, flag]).trust is decision


def test_graph_repository_is_checked_outside_checkout(monkeypatch):
    client = Mock()
    client.backend.project = 'default'
    client.get.side_effect = [
        {'autoApprove': False, 'defaultRepository': '/default'},
        {'definition': {'repository': '/graph-repo'}},
        {'name': 'owner/repo', 'repository': '/graph-repo', 'trusted': None},
    ]
    monkeypatch.setattr('sys.stdin.isatty', lambda: False)
    _check_trust(argparse.Namespace(graph='work'), client, '')
    assert client.get.call_args.args == ('/repositories/trust',)
    assert client.get.call_args.kwargs == {'repository': '/graph-repo'}
    client.post.assert_not_called()


@pytest.mark.parametrize('action,decision', [('trust', True), ('untrust', False)])
def test_repo_command_defaults_and_resolves_paths(tmp_path, monkeypatch, action, decision):
    from engine.cli import commands
    from engine.cli.backends import Backend
    client = Mock()
    client.post.return_value = {'trusted': decision}
    backend = Backend('local', 'http://localhost:4364')
    monkeypatch.setattr(commands, '_connect', lambda args: (backend, client))
    monkeypatch.chdir(tmp_path)
    assert commands.repo_trust(argparse.Namespace(repo='.', action=action)) == 0
    client.post.assert_called_once_with('/repositories/trust', {'repository': str(tmp_path), 'trusted': decision})
