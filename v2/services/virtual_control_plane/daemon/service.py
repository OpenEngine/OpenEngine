"""Standalone workspace daemon for configured executors; run inside a workspace runtime."""
from virtual_control_plane.domain.runtime import invalid
import argparse
import asyncio
import json
import logging
from pathlib import Path
import signal

import nats
from openengine.persistence import DatabaseConfig
from virtual_control_plane.daemon.executor import ExecutionWorker
from virtual_control_plane.daemon.steering import SteeringWorker
from virtual_control_plane.daemon.fake import FakeBackend
from virtual_control_plane.daemon.codex import CodexBackend
from virtual_control_plane.daemon.cgroups import CgroupFence
from virtual_control_plane.daemon.journal import Journal
from virtual_control_plane.daemon.initialization import Initialization
from virtual_control_plane.daemon.outbox import UpdateOutbox
from virtual_control_plane.persistence import PERSISTENCE
from virtual_control_plane.persistence.durable import DurableStore
from virtual_control_plane.service import StoreOwner, loop
from virtual_control_plane.transport.nats.runtime_protocol import Protocol, RuntimeTransport

logger = logging.getLogger(__name__)


async def serve(args, backends=None):
    owner = StoreOwner(args.journal_directory)
    connection = store = executor = None
    tasks, subscriptions = [], []
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    try:
        target = {'workspace_id': args.workspace_id, 'runtime_id': args.runtime_id, 'runtime_generation': args.generation}
        initialization = Initialization(args.journal_directory, target, args.initialize)
        config = DatabaseConfig(sqlite_directory=args.journal_directory)
        PERSISTENCE.upgrade(config)
        store = DurableStore(PERSISTENCE.database(config))
        await initialization.check(store)
        backends = backends if backends is not None else {'fake': FakeBackend()}
        if args.codex_acp:
            if not args.cgroup_root or args.agent_uid is None or args.agent_gid is None or not args.agent_home:
                invalid('Codex requires cgroup root, agent UID/GID and guest home configuration')
            fence = CgroupFence(args.cgroup_root, uid=args.agent_uid, gid=args.agent_gid, home=args.agent_home)
            backends['codex'] = CodexBackend([str(args.codex_acp.resolve(strict=True))], fence)
        journal = Journal(store, target, args.workspace_root, backends=backends)
        await journal.recover()
        initialization.finish()
        options = {'servers': [args.nats_url], 'max_reconnect_attempts': -1,
                   'inbox_prefix': f'_INBOX.oe.runtime.{args.runtime_id}'}
        if getattr(args, 'connection_file', None):
            import os
            info = args.connection_file.stat()
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                invalid('Runtime connection file must be private to the daemon user')
            config = json.loads(args.connection_file.read_text())
            if any(config[key] != value for key, value in
                   (('workspace_id', args.workspace_id), ('runtime_id', args.runtime_id), ('generation', args.generation))):
                invalid('Connection configuration binding differs')
            options.update(servers=[config['connection']['url']], user=config['connection']['user'],
                           password=config['connection']['password'])
        elif args.credentials:
            options['user_credentials'] = str(args.credentials)
        elif not args.local_account:
            invalid('Provision credentials or explicitly use an isolated local test account')
        connection = await nats.connect(**options)
        protocol = Protocol()

        async def receive(message):
            if not message.reply:
                return
            definition = message.subject.rsplit('.', 1)[1]
            try:
                body = protocol.decode(definition, message.data)
                response = await (journal.accept(message.subject, body) if definition in ('execute', 'cancel', 'steer') else journal.inspect(message.subject, body))
                await message.respond(protocol.encode('command_ack' if definition in ('execute', 'cancel', 'steer') else 'inspect_response', response))
            except (ValueError, UnicodeError):
                logger.warning('Discarded invalid runtime command')
            except Exception:
                logger.exception('Command not acknowledged; sender will retry')

        for action in ('execute', 'cancel', 'steer', 'inspect'):
            subscriptions.append(await connection.subscribe(f'oe.internal.v1.runtime.runtimes.{args.runtime_id}.{action}', cb=receive))
        await connection.flush()
        executor = ExecutionWorker(journal)
        tasks = [asyncio.create_task(loop(worker, stop)) for worker in (executor, SteeringWorker(journal), UpdateOutbox(journal, RuntimeTransport(connection)))]
        if args.ready_file:
            args.ready_file.write_text('listening')
        await stop.wait()
    finally:
        for subscription in subscriptions:
            await subscription.unsubscribe()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if executor:
            await executor.close()
        if connection:
            await connection.close()
        if store:
            await store.close()
        if args.ready_file:
            args.ready_file.unlink(missing_ok=True)
        owner.close()


def parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nats-url', default='nats://127.0.0.1:4222')
    parser.add_argument('--journal-directory', type=Path, required=True)
    parser.add_argument('--workspace-root', type=Path, required=True)
    parser.add_argument('--workspace-id', required=True)
    parser.add_argument('--runtime-id', required=True)
    parser.add_argument('--generation', type=int, required=True)
    parser.add_argument('--initialize', action='store_true')
    parser.add_argument('--local-account', action='store_true')
    parser.add_argument('--credentials', type=Path)
    parser.add_argument('--connection-file', type=Path, help='root-private provisioned connection configuration')
    parser.add_argument('--codex-acp', type=Path, help='preinstalled guest Codex ACP executable')
    parser.add_argument('--cgroup-root', type=Path)
    parser.add_argument('--agent-uid', type=int)
    parser.add_argument('--agent-gid', type=int)
    parser.add_argument('--agent-home', type=Path)
    parser.add_argument('--ready-file', type=Path)
    return parser


def main():
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve(parser().parse_args()))


if __name__ == '__main__':
    main()
