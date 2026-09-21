"""Repository inspection is available through MCP without write authority."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from engine.slack_concierge.repository import RepositoryReader
from engine.slack_concierge.slack_egress import ConciergeBroker, _mcp_response, tool_permission
from langgraph_acp.permissions import ACPPermissionOption, ACPPermissionRequest


def test_repository_listing_and_paged_read(tmp_path):
    (tmp_path / 'src').mkdir()
    source = tmp_path / 'src' / 'app.py'
    source.write_text('\n'.join(f'line {i}' for i in range(1, 251)))
    (tmp_path / '.env').write_text('secret')
    (tmp_path / 'link').symlink_to(source)
    reader = RepositoryReader(str(tmp_path))
    assert reader.call('list_repository_files', {}) == 'src/'
    assert reader.call('list_repository_files', {'path': 'src'}) == 'app.py'
    first = reader.call('read_repository_file', {'path': 'src/app.py'})
    assert first.startswith('1: line 1\n')
    assert first.endswith('200: line 200\n[Content truncated]')
    last = reader.call('read_repository_file', {'path': 'src/app.py', 'start_line': 201})
    assert last.startswith('201: line 201\n')
    assert last.endswith('250: line 250')
    assert source.read_text().startswith('line 1\n')


@pytest.mark.parametrize('path', ['../outside', '/etc/passwd', '.git/config', '.env', 'link', 'linked/file'])
def test_repository_rejects_paths_outside_its_visible_files(tmp_path, path):
    (tmp_path / 'link').symlink_to(tmp_path.parent / 'outside')
    (tmp_path / 'linked').symlink_to(tmp_path.parent, target_is_directory=True)
    reader = RepositoryReader(str(tmp_path))
    for name in ('read_repository_file', 'list_repository_files'):
        with pytest.raises(ValueError):
            reader.call(name, {'path': path})


def test_repository_limits_and_argument_validation(tmp_path):
    reader = RepositoryReader(str(tmp_path))
    source = tmp_path / 'source'
    for data in (b'\0binary', b'x' * (1024 * 1024 + 1), b'\xff'):
        source.write_bytes(data)
        with pytest.raises(ValueError):
            reader.call('read_repository_file', {'path': 'source'})
    source.write_text('x' * 40000)
    assert len(reader.call('read_repository_file', {'path': 'source'})) < 4100
    for arguments in ({}, {'path': 'source', 'start_line': True},
                      {'path': 'source', 'start_line': 0}, {'path': 'source', 'command': 'rm'}):
        with pytest.raises(ValueError):
            reader.call('read_repository_file', arguments)
    with pytest.raises(ValueError):
        reader.call('write_file', {'path': 'source'})
    for i in range(210):
        (tmp_path / f'file{i}').touch()
    assert len(reader.call('list_repository_files', {}).splitlines()) == 201


def test_repository_tools_over_mcp_and_permissions(tmp_path):
    (tmp_path / 'README.md').write_text('Repository guidance')

    async def scenario():
        create = AsyncMock()
        async with ConciergeBroker(create_workorder=create, default_repository=str(tmp_path)) as broker:
            config = broker.config
            args = config['args']
            port = int(args[args.index('--port') + 1])
            # Exercise the real stdio child and TCP broker, including advertised tools.
            child = await asyncio.create_subprocess_exec(
                config['command'], *args, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                requests = [
                    {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
                        'name': 'read_repository_file', 'arguments': {'path': 'README.md'}}},
                ]
                stdout, stderr = await asyncio.wait_for(child.communicate(
                    ''.join(json.dumps(r) + '\n' for r in requests).encode()), 20)
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
            assert child.returncode == 0, stderr.decode()
            listed, read = [json.loads(line)['result'] for line in stdout.splitlines()]
            names = {tool['name'] for tool in listed['tools']}
            assert names == {'create_workorder', 'list_repository_files', 'read_repository_file'}
            assert read['content'][0]['text'] == '1: Repository guidance'
            for name in ('git_subcommand', 'write_file', 'open_pull_request', 'Bash'):
                response = await _mcp_response('127.0.0.1', port, broker._token, {
                    'id': 3, 'method': 'tools/call', 'params': {'name': name, 'arguments': {}}})
                assert response['result']['isError'] is True
            (tmp_path / 'unicode.txt').write_text('😀' * 15000)
            bounded = await _mcp_response('127.0.0.1', port, broker._token, {
                'id': 4, 'method': 'tools/call', 'params': {
                    'name': 'read_repository_file', 'arguments': {'path': 'unicode.txt'}}})
            assert bounded['result']['content'][0]['text'].endswith('[Content truncated]')
            bad = await broker._submit({'token': 'wrong', 'name': 'read_repository_file',
                                        'arguments': {'path': 'README.md'}})
            assert bad['ok'] is False
            create.assert_not_awaited()
        for prefix in ('mcp__concierge__', 'concierge/'):
            for name in ('read_repository_file', 'list_repository_files', 'write_file', 'git_subcommand'):
                permission = await tool_permission(ACPPermissionRequest(
                    agent='codex', tool_call={'name': prefix + name},
                    options=(ACPPermissionOption('yes', kind='allow_once'),)))
                assert permission.granted == (name in names)
    asyncio.run(scenario())
