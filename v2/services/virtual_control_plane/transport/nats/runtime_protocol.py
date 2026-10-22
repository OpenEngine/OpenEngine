"""Internal wire validation and bounded request/reply; no state transitions."""
import json
import logging
import random
import time

from jsonschema import Draft7Validator, FormatChecker
from virtual_control_plane.transport.nats.validation import Contracts
from virtual_control_plane.domain.runtime import invalid

logger = logging.getLogger(__name__)


class Protocol:
    def __init__(self):
        contracts = Contracts()
        path = contracts.root / 'internal/workspace_runtime/v1/messages.schema.json'
        self.validators = {name: Draft7Validator({'$ref': path.resolve().as_uri() + '#/$defs/' + name},
                           registry=contracts.registry, format_checker=FormatChecker())
                           for name in ('execute', 'cancel', 'steer', 'command_ack', 'inspect', 'inspect_response', 'update', 'update_ack')}

    def encode(self, definition, value):
        self.validate(definition, value)
        wire = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
        if len(wire) > 1048576:
            invalid('Runtime message exceeds 1 MiB')
        return wire

    def decode(self, definition, wire):
        if len(wire) > 1048576:
            invalid('Runtime message exceeds 1 MiB')
        value = json.loads(wire.decode('utf-8'))
        self.validate(definition, value)
        return value

    def validate(self, definition, value):
        if next(self.validators[definition].iter_errors(value), None):
            invalid('Invalid internal runtime message')
        for cause in (value.get('cause'), value.get('execution', {}).get('cause')):
            if cause and cause['causal_path'][-1] != cause['operation']['command_id']:
                invalid('Invalid cause')
        if definition == 'execute' and value['command_id'] != value['execution']['command_id']:
            invalid('Invalid execute identity')
        if definition in ('cancel', 'steer') and value['command_id'] == value['execution']['command_id']:
            invalid('Control command must have its own identity')


class Backoff:
    def __init__(self):
        self.pending = {}

    def due(self, key):
        return self.pending.get(key, (0, 0))[1] <= time.monotonic()

    def failed(self, key):
        count = self.pending.get(key, (0, 0))[0]
        delay = min(10, .25 * 2 ** min(count, 6))
        self.pending[key] = (count + 1, time.monotonic() + random.uniform(delay / 2, delay))

    def done(self, key):
        self.pending.pop(key, None)


class RuntimeTransport:
    def __init__(self, connection):
        self.connection = connection
        self.protocol = Protocol()

    async def request(self, subject, wire, definition):
        message = await self.connection.request(subject, wire, timeout=5)
        return self.protocol.decode(definition, message.data)


class UpdateAdapter:
    def __init__(self, connection, handler, *, trusted=False):
        self.connection, self.handler, self.trusted = connection, handler, trusted
        self.protocol = Protocol()
        self.subscriptions = []

    async def start(self):
        if self.trusted:
            self.subscriptions.append(await self.connection.subscribe('oe.internal.v1.vcp.runtimes.*.updates', cb=self.receive))
            await self.connection.flush()

    async def receive(self, message):
        if not message.reply:
            return
        try:
            body = self.protocol.decode('update', message.data)
            response = await self.handler.accept(message.subject, body)
            await message.respond(self.protocol.encode('update_ack', response))
        except (ValueError, UnicodeError):
            logger.warning('Discarded invalid runtime update')
        except Exception:
            logger.exception('Runtime update not acknowledged; sender will retry')
