"""Synthetic rotation across real VM teardown and every copy-back boundary."""
# ruff: noqa: F811 -- shared imported fixtures are injected by pytest
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import shutil
import sys

import pytest

from engine.graph_runtime_langgraph.sandbox import SandboxACPProvider, SandboxConnectContext
from engine.graph_runtime_langgraph.sandbox_sessions import AgentSessions
from engine.graph_runtime_langgraph.sandbox_workspace import WorkspaceCopies, WorkspaceCredentialLeak
from engine.runtime.credential_rotation import credential_lease
from engine.runtime.model_credentials import host_model_credential, ModelCredentialError
from test_model_credentials import login_home  # noqa: F401 -- shared synthetic account fixture
from test_sandbox_sessions import backend  # noqa: F401 -- fake and actual VM backends

NEW_ACCESS = 'synthetic-rotated-access-private'
NEW_REFRESH = 'synthetic-rotated-refresh-private'


async def command(vm, code):
    process = await vm.exec(('python3', '-c', code))
    out, err = await asyncio.gather(process.stdout.read(), process.stderr.read())
    assert await process.wait() == 0, err
    return out


async def rotate(vm, mode='safe', access=NEW_ACCESS, refresh=NEW_REFRESH):
    await command(vm, f'''import json,os
from pathlib import Path
p=Path(os.environ['CODEX_HOME'])/'auth.json'
a=json.loads(p.read_text()); a['tokens'].update(access_token={access!r},refresh_token={refresh!r})
a['untrusted_guest_config']='do-not-import'
p.write_text(json.dumps(a))
mode={mode!r}
if mode in ('workspace','host-tool'):
 Path('leak.txt').write_text(a['tokens']['access_token'])
if mode in ('session','session-name'):
 d=p.parent/'sessions'; d.mkdir(exist_ok=True); (d/('leak.json' if mode=='session' else a['tokens']['refresh_token'])).write_text(a['tokens']['refresh_token'] if mode=='session' else 'session payload')
''')


@asynccontextmanager
async def connected(backend, tmp_path, *, sessions=None, node='one'):
    source = tmp_path / 'source'
    source.mkdir(exist_ok=True)
    shutil.copy2(Path(__file__).with_name('sandbox_acp_agent.py'), source / 'agent.py')
    (source / 'input.txt').write_text('workspace-marker')
    class Capture:
        @asynccontextmanager
        async def create(self, spec):
            async with backend.create(spec) as vm:
                self.vm = vm
                yield vm
    capture = Capture()
    async with WorkspaceCopies().create(str(source), 'apply') as workspace:
        provider = SandboxACPProvider(name='codex', model_provider='codex', command=('python3', 'agent.py'),
            backend=workspace.backend(capture), credential_source=host_model_credential, sessions=sessions)
        client = await provider.connect(context=SandboxConnectContext(workspace=str(source), run_id='run', node_id=node))
        try:
            yield client, capture.vm, workspace, source
        finally:
            await client.close()


@pytest.mark.parametrize('mode', ['workspace', 'host-tool', 'session', 'session-name'])
def test_refreshed_tokens_cannot_escape_through_exports(backend, login_home, tmp_path, mode):
    async def exercise():
        sessions = AgentSessions(tmp_path / 'sessions')
        async with connected(backend, tmp_path, sessions=sessions):
            pass
        checkpoint = next(sessions.directory.rglob('state.tar'))
        previous = checkpoint.read_bytes()
        if mode in ('session', 'session-name'):
            with pytest.raises(ValueError, match='credential') as error:
                async with connected(backend, tmp_path, sessions=sessions) as (_, vm, _, _):
                    await rotate(vm, mode)
            assert NEW_REFRESH not in str(error.value)
            assert checkpoint.read_bytes() == previous
        else:
            async with connected(backend, tmp_path, sessions=sessions) as (client, vm, workspace, source):
                await rotate(vm, mode)
                if mode == 'host-tool':
                    with pytest.raises(WorkspaceCredentialLeak):
                        async with workspace.host_operation():
                            pytest.fail('a leaked refreshed token reached a host mutation')
                await client.close()
                with pytest.raises(WorkspaceCredentialLeak) as error:
                    await workspace.accept()
                assert NEW_ACCESS not in str(error.value)
                assert not (source / 'leak.txt').exists()
        auth = json.loads((login_home / '.codex/auth.json').read_text())
        assert auth['tokens']['access_token'] == NEW_ACCESS
        assert auth['tokens']['refresh_token'] == NEW_REFRESH
        assert 'untrusted_guest_config' not in auth
        assert auth['tokens']['GITHUB_TOKEN'] == 'test-forge-token-676-never-forward'
        assert auth['github'] == 'test-forge-token-676-never-forward'
        assert not (login_home / '.codex/.openengine-refresh.active').exists()
    asyncio.run(exercise())


def test_next_and_parallel_nodes_receive_latest_rotation(backend, login_home, tmp_path):
    async def exercise():
        async with connected(backend, tmp_path) as (first, vm, _, _):
            await rotate(vm)
            # A second VM cannot be provisioned while the first account owner
            # may still rotate its single-use refresh token.
            second_scope = connected(backend, tmp_path, node='two')
            waiting = asyncio.create_task(second_scope.__aenter__())
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(waiting), .2)
            await first.close()
            second, vm2, _, _ = await asyncio.wait_for(waiting, 30)
            try:
                tokens = json.loads(await command(vm2, "import json,os;from pathlib import Path;print((Path(os.environ['CODEX_HOME'])/'auth.json').read_text())"))['tokens']
                assert tokens['access_token'] == NEW_ACCESS
                assert tokens['refresh_token'] == NEW_REFRESH
                await rotate(vm2, access='generation-three-access', refresh='generation-three-refresh')
            finally:
                await second_scope.__aexit__(None, None, None)
            assert second is not first
        auth = json.loads((login_home / '.codex/auth.json').read_text())
        assert auth['tokens']['refresh_token'] == 'generation-three-refresh'
        assert (login_home / '.codex/auth.json').stat().st_mode & 0o777 == 0o600
    asyncio.run(exercise())


@pytest.mark.parametrize('corruption', ['account', 'mode', 'invalid-json', 'oversized', 'symlink', 'deleted'])
def test_bad_guest_credentials_fail_closed_and_do_not_replace_host_login(backend, login_home, tmp_path, corruption):
    original = (login_home / '.codex/auth.json').read_bytes()
    async def exercise():
        with pytest.raises((ModelCredentialError, ValueError)):
            async with connected(backend, tmp_path) as (_, vm, workspace, _):
                await command(vm, f'''import json,os
from pathlib import Path
p=Path(os.environ['CODEX_HOME'])/'auth.json'; a=json.loads(p.read_text()); mode={corruption!r}
if mode=='account': a['tokens']['account_id']='someone-else'; p.write_text(json.dumps(a))
if mode=='mode': a['auth_mode']='apikey'; p.write_text(json.dumps(a))
if mode=='invalid-json': p.write_text('not-json')
if mode=='oversized': p.write_text('x'*65537)
if mode=='symlink': p.unlink(); p.symlink_to('/etc/passwd')
if mode=='deleted': p.unlink()
Path('output.txt').write_text('must not apply')
''')
                await workspace.export()
        assert (login_home / '.codex/auth.json').read_bytes() == original
        assert not (tmp_path / 'source/output.txt').exists()
        # Potentially used single-use credentials are never silently retried.
        with pytest.raises(ModelCredentialError, match='lost refresh ownership'):
            async with credential_lease(host_model_credential, 'codex', {}):
                pass
    asyncio.run(exercise())


def test_external_login_change_is_not_overwritten(login_home):
    async def exercise():
        async with credential_lease(host_model_credential, 'codex', {}) as (credential, state):
            observer = state.observe.__self__
            path = login_home / '.codex/auth.json'
            original = json.loads(path.read_text())
            replacement = {**original, 'tokens': {**original['tokens'], 'access_token': 'new-host-login'}}
            path.write_text(json.dumps(replacement))
            rotated = {**original, 'tokens': {**original['tokens'], 'access_token': NEW_ACCESS, 'refresh_token': NEW_REFRESH}}
            with pytest.raises(ModelCredentialError, match='changed outside'):
                observer.observe({'.codex/auth.json': json.dumps(rotated)})
            assert json.loads(path.read_text()) == replacement
    asyncio.run(exercise())


def test_cancelled_waiter_does_not_release_another_nodes_refresh_ownership(login_home):
    async def exercise():
        async with credential_lease(host_model_credential, 'codex', {}):
            async def contender():
                async with credential_lease(host_model_credential, 'codex', {}):
                    pytest.fail('lease overlap')
            task = asyncio.create_task(contender())
            await asyncio.sleep(.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert (login_home / '.codex/.openengine-refresh.active').exists()
        assert not (login_home / '.codex/.openengine-refresh.active').exists()
    asyncio.run(exercise())


@pytest.mark.skipif(sys.platform == 'win32', reason='SmolVM account ownership uses POSIX locks')
def test_process_ownership_and_host_crash_fail_closed(login_home, tmp_path):
    import subprocess
    from engine.ports.sandbox import command_environment
    ready = tmp_path / 'worker-ready'
    code = '''import asyncio,sys
from pathlib import Path
from engine.runtime.credential_rotation import credential_lease
from engine.runtime.model_credentials import host_model_credential
async def run():
 async with credential_lease(host_model_credential,'codex',{'CODEX_HOME':sys.argv[1]}) as (_,state):
  state.exposed=True
  Path(sys.argv[2]).write_text('owned')
  await asyncio.Event().wait()
asyncio.run(run())
'''
    async def exercise():
        async with credential_lease(host_model_credential, 'codex', {}):
            process = subprocess.Popen([sys.executable, '-c', code, str(login_home / '.codex'), str(ready)],
                env=command_environment(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                await asyncio.sleep(.2)
                assert not ready.exists()
            except BaseException:
                process.kill()
                process.wait()
                raise
        try:
            async with asyncio.timeout(10):
                while not ready.exists():
                    assert process.poll() is None, process.stderr.read()
                    await asyncio.sleep(.02)
            process.kill()
            await asyncio.to_thread(process.wait)
            with pytest.raises(ModelCredentialError, match='lost refresh ownership'):
                async with credential_lease(host_model_credential, 'codex', {}):
                    pass
        finally:
            if process.poll() is None:
                process.kill()
                await asyncio.to_thread(process.wait)
            process.stderr.close()
    asyncio.run(exercise())


def test_uncertain_rotation_after_live_checkpoint_is_not_reused(login_home):
    async def exercise():
        async with credential_lease(host_model_credential, 'codex', {}) as (credential, state):
            state.exposed = True
            data = json.loads(credential.value)
            data['tokens'].update(access_token=NEW_ACCESS, refresh_token=NEW_REFRESH)
            state.observe({'.codex/auth.json': json.dumps(data)})
            # No final snapshot: the VM may have rotated again after this
            # live checkpoint, then disappeared before exporting its last state.
        with pytest.raises(ModelCredentialError, match='lost refresh ownership'):
            async with credential_lease(host_model_credential, 'codex', {}):
                pass
        path = login_home / '.codex/auth.json'
        data = json.loads(path.read_text())
        data['tokens']['access_token'] = 'fresh-host-login'
        path.write_text(json.dumps(data))
        with pytest.raises(ModelCredentialError, match='lost refresh ownership'):
            async with credential_lease(host_model_credential, 'codex', {}):
                pass
        data['tokens']['refresh_token'] = 'fresh-host-refresh'
        path.write_text(json.dumps(data))
        async with credential_lease(host_model_credential, 'codex', {}) as (credential, _):
            assert json.loads(credential.value)['tokens']['access_token'] == 'fresh-host-login'
    asyncio.run(exercise())


@pytest.mark.parametrize('outcome', ['cancel', 'failure', 'timeout'])
def test_node_teardown_keeps_rotation_after_interruption(backend, login_home, tmp_path, outcome):
    async def exercise():
        ready = asyncio.Event()
        async def run():
            async with connected(backend, tmp_path) as (_, vm, _, _):
                await rotate(vm)
                ready.set()
                if outcome == 'failure':
                    message = 'synthetic node failure'
                    raise RuntimeError(message)
                async with asyncio.timeout(.05 if outcome == 'timeout' else None):
                    await asyncio.Event().wait()
        task = asyncio.create_task(run())
        await asyncio.wait_for(ready.wait(), 60)
        if outcome == 'cancel':
            task.cancel()
        expected = asyncio.CancelledError if outcome == 'cancel' else TimeoutError if outcome == 'timeout' else RuntimeError
        with pytest.raises(expected):
            await task
        async with credential_lease(host_model_credential, 'codex', {}) as (credential, _):
            assert json.loads(credential.value)['tokens']['refresh_token'] == NEW_REFRESH
        assert not (login_home / '.codex/.openengine-refresh.active').exists()
    asyncio.run(exercise())


def test_api_key_leases_remain_parallel_without_touching_login(login_home):
    path = login_home / '.codex/auth.json'
    original = path.read_bytes()
    async def exercise():
        values = {'OPENAI_API_KEY': 'synthetic-selected-api-key'}
        async with credential_lease(host_model_credential, 'codex', values):
            async def contender():
                async with credential_lease(host_model_credential, 'codex', values) as (credential, state):
                    state.observe({'.codex/auth.json': credential.home().files['.codex/auth.json']})
            await asyncio.wait_for(contender(), 1)
    asyncio.run(exercise())
    assert path.read_bytes() == original
    assert not (path.parent / '.openengine-refresh.lock').exists()


def test_rotation_history_remains_filtered_and_cannot_be_replayed(login_home):
    async def exercise():
        async with credential_lease(host_model_credential, 'codex', {}) as (credential, state):
            data = json.loads(credential.value)
            original_refresh = data['tokens']['refresh_token']
            data['tokens'].update(access_token=NEW_ACCESS, refresh_token=NEW_REFRESH)
            state.observe({'.codex/auth.json': json.dumps(data)})
            data['tokens'].update(access_token='later-access', refresh_token='later-refresh')
            state.observe({'.codex/auth.json': json.dumps(data)})
            assert {NEW_ACCESS, NEW_REFRESH, original_refresh, 'later-refresh'} <= set(state.redactions)
            data['tokens']['refresh_token'] = original_refresh
            with pytest.raises(ModelCredentialError, match='cannot be safely retained'):
                state.observe({'.codex/auth.json': json.dumps(data)})
            assert json.loads((login_home / '.codex/auth.json').read_text())['tokens']['refresh_token'] == 'later-refresh'
    asyncio.run(exercise())


def test_invalid_rotation_still_adds_its_tokens_to_redactions(login_home):
    from engine.runtime.credential_rotation import CredentialObserver
    credential = host_model_credential('codex', {})
    observer = CredentialObserver(credential)
    data = json.loads(credential.value)
    data['tokens'].update(account_id='foreign-account', access_token=NEW_ACCESS, refresh_token=NEW_REFRESH)
    with pytest.raises(ModelCredentialError):
        observer.observe({'.codex/auth.json': json.dumps(data)})
    assert {NEW_ACCESS, NEW_REFRESH} <= set(observer.state.redactions)
    assert observer.current == credential


@pytest.mark.parametrize('failed_write', ['marker', 'auth'])
def test_quarantine_is_written_before_auth_and_failed_writes_do_not_advance_state(login_home, monkeypatch, failed_write):
    import engine.runtime.credential_rotation as rotation
    credential = host_model_credential('codex', {})
    marker = login_home / '.codex/.openengine-refresh.active'
    observer = rotation.CredentialObserver(credential, marker)
    auth = credential.source_path
    original = auth.read_bytes()
    writes = []
    replace = rotation._replace
    def write(path, value):
        writes.append(path)
        if path == (marker if failed_write == 'marker' else auth):
            message = 'synthetic write failure'
            raise OSError(message)
        replace(path, value)
    monkeypatch.setattr(rotation, '_replace', write)
    data = json.loads(credential.value)
    data['tokens'].update(access_token=NEW_ACCESS, refresh_token=NEW_REFRESH)
    with pytest.raises(OSError, match='synthetic write failure'):
        observer.observe({'.codex/auth.json': json.dumps(data)})
    assert writes == ([marker] if failed_write == 'marker' else [marker, auth])
    assert auth.read_bytes() == original
    assert observer.current == credential
    assert NEW_REFRESH in observer.state.redactions
    if failed_write == 'auth':
        import hashlib
        assert hashlib.sha256(NEW_REFRESH.encode()).hexdigest() in json.loads(marker.read_text())
