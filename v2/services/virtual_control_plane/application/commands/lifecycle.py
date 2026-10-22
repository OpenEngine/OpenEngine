"""Durable acceptance; no provider calls occur on the reply path."""
import hashlib
import json
from datetime import datetime

from virtual_control_plane.application.ports import Authorizer, Compute, ExecutionContext, RuntimeCommandEncoder, Storage
from virtual_control_plane.application.commands.execution import accept_execute
from virtual_control_plane.application.commands.cancellation import accept_cancel
from virtual_control_plane.application.commands.steering import accept_steer
from virtual_control_plane.domain.lifecycle import Rejected, check_action, event, identity, now, unsupported, validate_create


def fingerprint(subject, request):
    # deadline_at is an acceptance constraint, deliberately not retry identity.
    content = {key: request.get(key) for key in ('payload', 'expected_version', 'causal_path')}
    return hashlib.sha256(json.dumps([subject, content], sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class Commands:
    def __init__(self, storage: Storage, providers: dict[str, Compute], authorizer: Authorizer, execution_context: ExecutionContext | None = None,
                 runtime_encoder: RuntimeCommandEncoder | None = None):
        self.storage, self.providers, self.authorizer = storage, providers, authorizer
        self.execution_context = execution_context
        self.runtime_encoder = runtime_encoder

    async def accept(self, subject, action, target, request, actor, validation_error=None):
        command_id = request['command_id']
        digest = fingerprint(subject, request)
        async with self.storage.transaction() as tx:
            try:
                return await self._accept(tx, subject, action, target, request, actor, digest, validation_error)
            except Rejected as error:
                response = {'command_id': command_id, 'status': 'rejected', 'error': error.error}
                # Conflicts and unauthorized requests must never overwrite the receipt.
                if error.error['retryable'] or error.error['code'] in ('COMMAND_ID_CONFLICT', 'PERMISSION_DENIED'):
                    return response
            await tx.put('command_receipts', command_id, {'owner': actor, 'fingerprint': digest,
                         'response': response, 'created_at': now()})
            return response

    async def _accept(self, tx, subject, action, target, request, actor, digest, validation_error):
        command_id = request['command_id']
        await self.authorizer.authorize(actor, subject, None)
        receipt = await tx.get('command_receipts', command_id)
        if receipt:
            await self.authorizer.authorize(actor, subject, receipt['owner'])
            if receipt['fingerprint'] != digest:
                raise Rejected('COMMAND_ID_CONFLICT', 'Command ID was already used', command_id=command_id)
            return receipt['response']
        record = await tx.get('workspace_records', target) if target else None
        if record:
            await self.authorizer.authorize(actor, subject, record['owner'])
        if validation_error:
            raise validation_error
        timestamp = now()
        if request.get('deadline_at') and datetime.fromisoformat(request['deadline_at'].replace('Z', '+00:00')) <= datetime.fromisoformat(timestamp.replace('Z', '+00:00')):
            raise Rejected('DEADLINE_EXCEEDED', 'Acceptance deadline passed',
                           deadline_at=request['deadline_at'], received_at=timestamp)
        if len(request['causal_path']) >= 32:
            raise Rejected('CAUSAL_PATH_TOO_LONG', 'No room for completion events', max_length=32, length=len(request['causal_path']) + 1)
        payload = request['payload']
        if action == 'create':
            if 'expected_version' in request:
                raise Rejected('INVALID_ARGUMENT', 'Create has no existing resource version')
            provider = self.providers.get(payload['provider'])
            if provider is None:
                unsupported('provider', payload['provider'])
            validate_create(payload, provider.capabilities)
            provider.validate(payload)
            target = identity('ws')
            workspace = {'workspace_id': target, 'version': 1, 'provider': payload['provider'],
                         'state': 'provisioning', 'resources': payload.get('resources', {}),
                         'tools': payload.get('tools', []), 'required_capabilities': payload.get('required_capabilities', []),
                         'labels': payload.get('labels', {}), 'active_attempt_ids': [],
                         'created_at': timestamp, 'updated_at': timestamp}
            record = {'owner': actor, 'workspace': workspace, 'specification': payload}
        else:
            if record is None:
                raise Rejected('NOT_FOUND', 'Workspace does not exist')
            workspace = record['workspace']
            if 'expected_version' in request and request['expected_version'] != workspace['version']:
                raise Rejected('VERSION_CONFLICT', 'Workspace version changed', expected_version=request['expected_version'], actual_version=workspace['version'])
            if record.get('active_operation') and action != 'cancel':
                raise Rejected('UNAVAILABLE', 'Another workspace operation is in progress')
            if action in ('cancel', 'steer'):
                handler = accept_cancel if action == 'cancel' else accept_steer
                response = await handler(tx, record, request, actor, timestamp, self.runtime_encoder)
                await tx.put('command_receipts', command_id, {'owner': actor, 'fingerprint': digest,
                             'response': response, 'created_at': timestamp})
                return response
            if action == 'execute':
                response = await accept_execute(tx, record, request, actor, timestamp,
                                                self.execution_context, self.runtime_encoder, self.authorizer, subject)
                await tx.put('command_receipts', command_id, {'owner': actor, 'fingerprint': digest,
                             'response': response, 'created_at': timestamp})
                return response
            check_action(action, workspace, payload)
        operation_id = identity('op')
        operation = {'operation_id': operation_id, 'command_id': command_id,
                     'operation': f'workspaces.{action}', 'status': 'pending',
                     'resources': {'workspace_id': target}, 'created_at': timestamp, 'updated_at': timestamp}
        response = {'command_id': command_id, 'status': 'accepted', 'operation_id': operation_id, 'resource_id': target}
        record['active_operation'] = operation_id
        if action == 'create':
            runtime_id = identity('rt')
            await tx.put('runtime_ownership', target, {'workspace_id': target, 'runtime_id': runtime_id,
                         'generation': 1, 'name': f'oe-vcp-{runtime_id}', 'provider': workspace['provider']})
            outgoing = event(workspace, operation, request, 'workspace.provisioning', {'provider': workspace['provider']})
            await tx.put('outbox', outgoing['event_id'], {'event': outgoing, 'published': False})
        elif action == 'destroy' and workspace['state'] != 'destroyed':
            workspace.update(state='destroying', version=workspace['version'] + 1, updated_at=timestamp)
        await tx.put('workspace_records', target, record)
        await tx.put('operations', operation_id, {'owner': actor, 'operation': operation, 'request': request})
        await tx.put('command_receipts', command_id, {'owner': actor, 'fingerprint': digest,
                     'response': response, 'created_at': now()})
        return response
