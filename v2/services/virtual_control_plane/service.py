"""Composition and local lifecycle. One owner per SQLite database."""
import argparse
import asyncio
import fcntl
import json
import logging
import os
from pathlib import Path
import signal

import nats
from nats.js.errors import NotFoundError
from openengine.persistence import DatabaseConfig
from virtual_control_plane.application.runtime_updates import RuntimeUpdates
from virtual_control_plane.application.runtime_context import FakeRuntimeContext, RuntimeContext
from virtual_control_plane.transport.nats.runtime_protocol import RuntimeTransport, UpdateAdapter
from virtual_control_plane.workers.runtime_commands import RuntimeDispatcher
from virtual_control_plane.application.auth import LocalAccount
from virtual_control_plane.application.commands.lifecycle import Commands
from virtual_control_plane.application.queries.read import Queries
from virtual_control_plane.persistence import PERSISTENCE
from virtual_control_plane.persistence.durable import DurableStore
from virtual_control_plane.providers.smolvm.provider import Smolvm
from virtual_control_plane.transport.nats.adapter import Adapter, JetStreamPublisher
from virtual_control_plane.transport.nats.runtime_commands import RuntimeCommands
from virtual_control_plane.workers.operations import OperationWorker
from virtual_control_plane.workers.outbox import OutboxPublisher

logger = logging.getLogger(__name__)


class StoreOwner:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.file = (directory / 'vcp.lock').open('a')
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            message = 'Another VCP owns this database'
            raise RuntimeError(message) from None

    def close(self):
        self.file.close()


async def loop(worker, stop):
    while not stop.is_set():
        try:
            if await worker.run_once():
                continue
        except Exception:
            logger.exception('worker failed; durable work will be retried')
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.25)
        except TimeoutError:
            pass


async def serve(arguments, providers=None, execution_context=None):
    config = DatabaseConfig.from_environment()
    owner = StoreOwner(config.sqlite_directory)
    connection = None
    runtime_connection = runtime_broker = None
    store = None
    server = None
    tasks = []
    stop = asyncio.Event()
    event_loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        event_loop.add_signal_handler(sig, stop.set)
    try:
        PERSISTENCE.upgrade(config)
        store = DurableStore(PERSISTENCE.database(config))
        runtime = None
        if getattr(arguments, 'runtime_bundle', None):
            from virtual_control_plane.provisioning.broker import RuntimeBroker
            from virtual_control_plane.provisioning.runtime import RuntimeProvisioner
            if not arguments.image or not arguments.runtime_broker_binary:
                message = 'Automatic runtime provisioning requires image and runtime broker binary'
                raise ValueError(message)
            runtime_broker = RuntimeBroker(config.sqlite_directory / 'runtime-broker',
                                           arguments.runtime_broker_binary, arguments.runtime_port,
                                           arguments.runtime_guest_url, arguments.runtime_bind_address)
            runtime_connection = await runtime_broker.start()
            runtime = RuntimeProvisioner(runtime_broker, runtime_connection, arguments.runtime_bundle,
                                         arguments.runtime_adapter, arguments.runtime_egress_host)
            execution_context = RuntimeContext()
        providers = providers if providers is not None else ({'smolvm': Smolvm(arguments.image, runtime=runtime)} if arguments.image else {})
        connection = await nats.connect(arguments.nats_url, max_reconnect_attempts=-1, inbox_prefix='_INBOX.oe.vcp.local')
        if getattr(arguments, 'fake_executor', False):
            execution_context = FakeRuntimeContext()
        if getattr(arguments, 'codex_executor', False):
            execution_context = RuntimeContext(('codex', 'fake') if arguments.fake_executor else ('codex',))
        jetstream = connection.jetstream()
        try:
            await jetstream.stream_info('vcp-events')
        except NotFoundError:
            await jetstream.add_stream(name='vcp-events', subjects=['oe.v1.vcp.events.>'], storage='file',
                                       retention='limits', discard='new', max_age=604800, max_bytes=1073741824,
                                       duplicate_window=120, num_replicas=1)
        auth = LocalAccount()
        adapter = Adapter(connection, Commands(store, providers, auth, execution_context, RuntimeCommands()), Queries(store, providers, auth),
                          local_account=arguments.local_account)
        await adapter.start()
        runtime_adapter = UpdateAdapter(runtime_connection or connection, RuntimeUpdates(store),
                                        trusted=bool(runtime_broker) or arguments.local_account)
        await runtime_adapter.start()
        tasks = [asyncio.create_task(loop(worker, stop)) for worker in (
            OperationWorker(store, providers), RuntimeDispatcher(store, RuntimeTransport(runtime_connection or connection)), OutboxPublisher(store, JetStreamPublisher(jetstream)))]

        async def health(reader, writer):
            try:
                await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), timeout=2)
                healthy = connection.is_connected and (runtime_connection is None or runtime_connection.is_connected) and all(not task.done() for task in tasks) and not stop.is_set()
                status = b'200 OK' if healthy else b'503 Unavailable'
                writer.write(b'HTTP/1.1 ' + status + b'\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                await writer.drain()
            except (TimeoutError, ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                pass
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(health, '127.0.0.1', arguments.health_port)
        if arguments.ready_file:
            arguments.ready_file.write_text(f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/healthz')
        logger.info('VCP ready')
        await stop.wait()
    finally:
        stop.set()
        if server:
            server.close()
            await server.wait_closed()
        if connection:
            # Stop accepting new work before stopping workers. Accepted records
            # are durable even when the caller loses its reply during shutdown.
            subscriptions = adapter.subscriptions if 'adapter' in locals() else []
            subscriptions += runtime_adapter.subscriptions if 'runtime_adapter' in locals() else []
            for subscription in subscriptions:
                await subscription.unsubscribe()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if connection:
            await connection.close()
        if runtime_connection:
            await runtime_connection.close()
        if runtime_broker:
            await runtime_broker.close()
        if store:
            await store.close()
        if arguments.ready_file:
            arguments.ready_file.unlink(missing_ok=True)
        owner.close()


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument('--nats-url', default=os.environ.get('OE_NATS_URL', 'nats://127.0.0.1:4222'))
    result.add_argument('--image', default=os.environ.get('OE_VCP_SMOLVM_IMAGE'))
    result.add_argument('--local-account', action='store_true', help='trust all publishers in this dedicated local NATS account')
    result.add_argument('--codex-executor', action='store_true', help='enable Codex execution after daemon registration')
    result.add_argument('--fake-executor', action='store_true', help='enable fake execution after daemon registration')
    result.add_argument('--runtime-bind-address', help='host interface for the authenticated runtime broker; defaults to loopback')
    result.add_argument('--runtime-egress-host', action='append', default=[], help='additional guest egress hostname; repeat for model/API hosts')
    result.add_argument('--runtime-bundle', type=Path, help='locked guest wheel bundle; enables automatic daemon provisioning')
    result.add_argument('--runtime-broker-binary', help='path to nats-server for the private internal broker')
    result.add_argument('--runtime-port', type=int, default=4422)
    result.add_argument('--runtime-guest-url', help='guest-reachable address of the internal broker')
    result.add_argument('--runtime-adapter', default='/usr/local/bin/codex-acp', help='absolute guest adapter path; /opt/oe/fixture only for test bundles')
    result.add_argument('--health-port', type=int, default=0)
    result.add_argument('--ready-file', type=Path)
    return result


class StructuredLog(logging.Formatter):
    def format(self, record):
        value = {'level': record.levelname, 'logger': record.name, 'message': record.getMessage()}
        for key in ('workspace_id', 'operation_id', 'causal_path'):
            if hasattr(record, key):
                value[key] = getattr(record, key)
        if record.exc_info:
            value['exception'] = self.formatException(record.exc_info)
        return json.dumps(value)


def main():
    handler = logging.StreamHandler()
    handler.setFormatter(StructuredLog())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    asyncio.run(serve(parser().parse_args()))


if __name__ == '__main__':
    main()
