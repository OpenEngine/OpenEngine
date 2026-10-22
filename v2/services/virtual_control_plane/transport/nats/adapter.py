"""NATS objects stop here; application code receives ordinary values."""
import json
import logging
from virtual_control_plane.domain.lifecycle import Rejected
from virtual_control_plane.transport.nats.validation import Contracts

logger = logging.getLogger(__name__)


class Adapter:
    def __init__(self, connection, commands, queries, *, local_account=False):
        self.connection, self.commands, self.queries = connection, commands, queries
        self.contracts = Contracts()
        # Explicit deployment trust boundary. NATS headers are NOT actor proof.
        self.actor = 'local' if local_account else None
        self.subscriptions = []

    async def start(self):
        for subject in ('oe.v1.vcp.control.>', 'oe.v1.vcp.query.>'):
            self.subscriptions.append(await self.connection.subscribe(subject, cb=self.receive))
        await self.connection.flush()

    async def receive(self, message):
        if not message.reply:
            return
        request = {}
        control = message.subject.startswith('oe.v1.vcp.control.')
        try:
            request = json.loads(message.data)
            response = await self.handle(message.subject, request, control)
        except (Rejected, ValueError, UnicodeError) as error:
            detail = error.error if isinstance(error, Rejected) else {'code': 'INVALID_ARGUMENT', 'message': 'Invalid JSON', 'retryable': False}
            response = {'status': 'rejected' if control else 'error', 'error': detail}
            if control:
                # Malformed envelopes may have no valid ID to echo. The public
                # contract cannot represent that case; see README protocol gaps.
                response['command_id'] = request.get('command_id', 'cmd_invalid') if isinstance(request, dict) else 'cmd_invalid'
            elif isinstance(request, dict) and 'request_id' in request:
                response['request_id'] = request['request_id']
        except Exception:
            logger.exception('request failed')
            response = {'status': 'rejected' if control else 'error', 'error': {
                'code': 'UNAVAILABLE', 'message': 'VCP temporarily unavailable', 'retryable': True}}
            if control:
                response['command_id'] = request.get('command_id', 'cmd_invalid')
            elif 'request_id' in request:
                response['request_id'] = request['request_id']
        await message.respond(json.dumps(response).encode())

    def validate_request(self, control, schema, request):
        self.contracts.validate('commands' if control else 'queries', schema, request)
        if control and request['causal_path'][-1] != request['command_id']:
            raise Rejected('INVALID_ARGUMENT', 'causal_path must end with command_id')

    async def handle(self, subject, request, control):
        if not isinstance(request, dict):
            raise Rejected('INVALID_ARGUMENT', 'Request must be an object')
        kind, action, target, schema = self.contracts.route(subject)
        validation_error = None
        try:
            self.validate_request(control, schema, request)
        except Rejected as error:
            validation_error = error
        if kind == 'command' and self.contracts.command_id(request.get('command_id')):
            response = await self.commands.accept(subject, action, target, request, self.actor, validation_error)
        elif validation_error:
            raise validation_error
        elif kind == 'command':
            raise Rejected('INVALID_ARGUMENT', 'Invalid command ID')
        else:
            response = await self.queries.get(subject, action, target, request, self.actor)
        return response


class JetStreamPublisher:
    def __init__(self, jetstream):
        self.jetstream = jetstream

    async def publish(self, event):
        subject = f"oe.v1.vcp.events.workspaces.{event['resources']['workspace_id']}.{event['event_type']}"
        await self.jetstream.publish(subject, json.dumps(event).encode(), headers={'Nats-Msg-Id': event['event_id']})
