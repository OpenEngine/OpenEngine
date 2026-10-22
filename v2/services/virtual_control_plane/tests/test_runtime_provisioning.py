import asyncio
import json
import os
import socket

import nats
import pytest

from virtual_control_plane.provisioning.broker import RuntimeBroker
from virtual_control_plane.provisioning.runtime import RuntimeProvisioner


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@pytest.mark.skipif(not os.environ.get('NATS_SERVER'), reason='Set NATS_SERVER for scoped runtime broker tests')
def test_runtime_credentials_scope_replies_restart_and_revocation(tmp_path):
    async def scenario():
        broker = RuntimeBroker(tmp_path, os.environ['NATS_SERVER'], port())
        control = await broker.start()
        runtime = None
        errors = []
        ownership = {'workspace_id': 'ws_one', 'runtime_id': 'rt_one', 'generation': 1}
        try:
            granted = await broker.grant(ownership)
            await asyncio.sleep(.2)

            async def on_error(error):
                errors.append(error)

            runtime = await nats.connect(broker.url, user=granted['user'], password=granted['password'],
                                         inbox_prefix='_INBOX.oe.runtime.rt_one', error_cb=on_error,
                                         max_reconnect_attempts=0)

            async def reply(message):
                await message.respond(b'committed')

            await control.subscribe('oe.internal.v1.vcp.runtimes.rt_one.updates', cb=reply)
            await control.flush()
            assert (await runtime.request('oe.internal.v1.vcp.runtimes.rt_one.updates', b'update', timeout=2)).data == b'committed'
            await runtime.subscribe('oe.internal.v1.runtime.runtimes.rt_one.inspect', cb=reply)
            await runtime.flush()
            assert (await control.request('oe.internal.v1.runtime.runtimes.rt_one.inspect', b'inspect', timeout=2)).data == b'committed'
            await runtime.subscribe('oe.internal.v1.runtime.runtimes.rt_other.execute')
            await runtime.publish('oe.internal.v1.vcp.runtimes.rt_other.updates', b'forged')
            await runtime.publish('oe.v1.vcp.control.workspaces.create', b'forged')
            await runtime.flush()
            await asyncio.sleep(.1)
            assert len(errors) >= 3
            assert all(isinstance(error, nats.errors.Error) for error in errors)
            assert tmp_path.stat().st_mode & 0o077 == 0
            assert (tmp_path / 'identities.json').stat().st_mode & 0o077 == 0
            await runtime.close()
            await control.close()
            await broker.close()
            broker = RuntimeBroker(tmp_path, os.environ['NATS_SERVER'], broker.port)
            control = await broker.start()
            assert await broker.grant(ownership) == granted
            with pytest.raises(ValueError, match='endpoint'):
                RuntimeBroker(tmp_path, os.environ['NATS_SERVER'], broker.port + 1)
            with pytest.raises(ValueError, match='binding'):
                await broker.grant({**ownership, 'generation': 2})
            await broker.revoke(ownership)
            assert 'rt_one' not in json.loads((tmp_path / 'identities.json').read_text())['runtimes']
            assert 'rt_one' not in json.dumps(broker.configuration())
        finally:
            if runtime:
                await runtime.close()
            await control.close()
            await broker.close()
    asyncio.run(scenario())


def test_bundle_must_exist_and_adapter_is_operator_path(tmp_path):
    with pytest.raises(ValueError, match='absolute'):
        RuntimeProvisioner(None, None, tmp_path, 'codex-acp')
    with pytest.raises(ValueError, match='incomplete'):
        RuntimeProvisioner(None, None, tmp_path, '/usr/bin/codex-acp')
    with pytest.raises(FileNotFoundError):
        RuntimeProvisioner(None, None, tmp_path / 'missing', '/usr/bin/codex-acp')



def test_bootstrap_retry_preserves_install_and_rejects_configuration_drift(tmp_path, monkeypatch):
    from virtual_control_plane.provisioning import guest

    installed = tmp_path / 'installed.json'
    incoming = tmp_path / 'incoming.json'
    configuration = {'workspace_id': 'ws_one', 'runtime_id': 'rt_one', 'generation': 1,
                     'adapter': '/opt/adapter', 'connection': {'password': 'test-only'}}
    installed.write_text(json.dumps(configuration))
    incoming.write_text(json.dumps(configuration))
    monkeypatch.setattr(guest, 'CONFIG', installed)
    monkeypatch.setattr(guest.os, 'geteuid', lambda: 0)

    def unexpected_install(*args, **kwargs):
        pytest.fail('Bootstrap retry must not replace an already installed runtime')

    monkeypatch.setattr(guest.subprocess, 'run', unexpected_install)
    guest.bootstrap(tmp_path, incoming)
    assert not incoming.exists()
    assert json.loads(installed.read_text()) == configuration
    for changed in ({'generation': 2}, {'connection': {'password': 'different'}}, {'adapter': '/opt/another'}):
        incoming.write_text(json.dumps({**configuration, **changed}))
        with pytest.raises(ValueError, match='binding'):
            guest.bootstrap(tmp_path, incoming)
        assert json.loads(installed.read_text()) == configuration


@pytest.mark.parametrize('behavior', ['exit', 'hang'])
def test_broker_startup_is_bounded_and_releases_ownership(tmp_path, behavior):
    import time

    executable = tmp_path / 'broker'
    executable.write_text('#!/bin/sh\n' + ('exit 1\n' if behavior == 'exit' else 'exec sleep 60\n'))
    executable.chmod(0o700)

    async def scenario():
        broker = RuntimeBroker(tmp_path / 'state', str(executable), port())
        broker.startup_timeout = .5
        started = time.monotonic()
        with pytest.raises(RuntimeError, match='failed to start'):
            await broker.start()
        assert time.monotonic() - started < 5
        assert broker.process.returncode is not None
        # No surviving child or held lock can obstruct the next start.
        replacement = RuntimeBroker(broker.directory, str(executable), broker.port)
        replacement.startup_timeout = .5
        with pytest.raises(RuntimeError, match='failed to start'):
            await replacement.start()
    asyncio.run(scenario())


@pytest.mark.skipif(not os.environ.get('NATS_SERVER'), reason='Set NATS_SERVER for crash recovery tests')
def test_hard_owner_crash_reaps_broker_and_recovers_credentials(tmp_path):
    import subprocess
    import sys
    import time

    directory = tmp_path / 'state'
    selected_port = port()
    ready = tmp_path / 'ready'
    script = '''
import asyncio, sys
from pathlib import Path
from virtual_control_plane.provisioning.broker import RuntimeBroker
async def main():
    broker = RuntimeBroker(sys.argv[1], sys.argv[2], int(sys.argv[3]))
    connection = await broker.start()
    await broker.grant({'workspace_id': 'ws_one', 'runtime_id': 'rt_one', 'generation': 1})
    Path(sys.argv[4]).touch()
    await asyncio.Event().wait()
asyncio.run(main())
'''
    with (tmp_path / 'owner.log').open('w') as log:
        owner = subprocess.Popen([sys.executable, '-c', script, str(directory), os.environ['NATS_SERVER'],
                                  str(selected_port), str(ready)], stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 10
            while not ready.exists():
                assert owner.poll() is None
                assert time.monotonic() < deadline
                time.sleep(.02)
            saved = json.loads((directory / 'identities.json').read_text())['runtimes']['rt_one']
            owner.kill()
            owner.wait(timeout=5)

            async def recover():
                broker = RuntimeBroker(directory, os.environ['NATS_SERVER'], selected_port)
                connection = await broker.start()
                runtime = None
                try:
                    assert broker.state['runtimes']['rt_one'] == saved
                    granted = await broker.grant({'workspace_id': 'ws_two', 'runtime_id': 'rt_two', 'generation': 1})
                    await asyncio.sleep(.2)
                    runtime = await nats.connect(broker.url, user=granted['user'], password=granted['password'],
                                                 inbox_prefix='_INBOX.oe.runtime.rt_two', max_reconnect_attempts=1)
                    await runtime.flush(timeout=1)
                    await broker.revoke({'runtime_id': 'rt_two'})
                finally:
                    if runtime:
                        await runtime.close()
                    await connection.close()
                    await broker.close()
            asyncio.run(recover())
        finally:
            if owner.poll() is None:
                owner.kill()
                owner.wait(timeout=5)


@pytest.mark.skipif(not os.environ.get('NATS_SERVER'), reason='Set NATS_SERVER for listener ownership tests')
def test_existing_listener_is_not_adopted_or_signalled(tmp_path):
    import subprocess
    from virtual_control_plane.provisioning.broker import private_json

    async def scenario():
        original = RuntimeBroker(tmp_path, os.environ['NATS_SERVER'], port())
        private_json(original.config, original.configuration())
        with (tmp_path / 'original.log').open('w') as log:
            listener = subprocess.Popen([original.executable, '-c', str(original.config)], stdout=log, stderr=log)
            connection = None
            try:
                async with asyncio.timeout(3):
                    while connection is None:
                        try:
                            connection = await original.connect()
                        except (OSError, nats.errors.Error):
                            await asyncio.sleep(.05)
                replacement = RuntimeBroker(tmp_path, original.executable, original.port)
                replacement.startup_timeout = 1
                with pytest.raises(RuntimeError, match='failed to start'):
                    await replacement.start()
                assert listener.poll() is None
                await connection.flush(timeout=1)
            finally:
                if connection:
                    await connection.close()
                listener.terminate()
                listener.wait(timeout=5)
    asyncio.run(scenario())


def test_startup_deadline_covers_unresponsive_nats_handshake(tmp_path):
    import time

    executable = tmp_path / 'broker'
    executable.write_text('#!/bin/sh\nexec sleep 60\n')
    executable.chmod(0o700)

    async def scenario():
        writers = []

        def connected(reader, writer):
            writers.append(writer)  # Accept TCP but never send NATS INFO.

        server = await asyncio.start_server(connected, '127.0.0.1', 0)
        broker = RuntimeBroker(tmp_path / 'state', str(executable), server.sockets[0].getsockname()[1])
        broker.startup_timeout = .3
        started = time.monotonic()
        try:
            with pytest.raises(RuntimeError, match='failed to start'):
                await broker.start()
            assert time.monotonic() - started < 3
            assert broker.process.returncode is not None
        finally:
            server.close()
            for writer in writers:
                writer.close()
                await writer.wait_closed()
            await server.wait_closed()
    asyncio.run(scenario())


@pytest.mark.skipif(not os.environ.get('NATS_SERVER'), reason='Set NATS_SERVER for reconnect tests')
def test_control_reconnect_restores_commands_updates_and_reply_inboxes(tmp_path):
    async def scenario():
        broker = RuntimeBroker(tmp_path, os.environ['NATS_SERVER'], port())
        control = await broker.start()
        runtime = None
        reconnected = asyncio.Event()
        disconnected = asyncio.Event()
        try:
            granted = await broker.grant({'workspace_id': 'ws_one', 'runtime_id': 'rt_one', 'generation': 1})
            await asyncio.sleep(.2)
            runtime = await nats.connect(broker.url, user=granted['user'], password=granted['password'],
                                         inbox_prefix='_INBOX.oe.runtime.rt_one', max_reconnect_attempts=1)

            async def reply(message):
                await message.respond(b'committed')

            async def on_disconnect():
                disconnected.set()

            async def on_reconnect():
                reconnected.set()

            # Test-only fault injection cuts the socket while the broker stays up.
            control._disconnected_cb = on_disconnect
            control._reconnected_cb = on_reconnect
            await control.subscribe('oe.internal.v1.vcp.runtimes.rt_one.updates', cb=reply)
            await runtime.subscribe('oe.internal.v1.runtime.runtimes.rt_one.inspect', cb=reply)
            await control.flush()
            await runtime.flush()
            subject = 'oe.internal.v1.runtime.runtimes.rt_one.inspect'
            assert (await control.request(subject, b'inspect', timeout=2)).data == b'committed'
            control._transport.close()
            async with asyncio.timeout(5):
                await disconnected.wait()
                await reconnected.wait()
            assert not control.is_closed
            assert (await control.request(subject, b'inspect', timeout=2)).data == b'committed'
            assert (await runtime.request('oe.internal.v1.vcp.runtimes.rt_one.updates', b'update', timeout=2)).data == b'committed'
        finally:
            if runtime:
                await runtime.close()
            await control.close()
            await broker.close()
    asyncio.run(scenario())
