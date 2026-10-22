"""One bounded worker per exclusively owned store; running work resumes on restart."""
import logging
from virtual_control_plane.domain.lifecycle import event, now

logger = logging.getLogger(__name__)


class ProviderUnavailable(RuntimeError):
    def __init__(self, provider):
        super().__init__(f"Provider {provider!r} is not configured; restore its configuration and submit a new command")


async def execute(provider, action, ownership, workspace_record, request):
    if provider is None:
        raise ProviderUnavailable(workspace_record['workspace']['provider'])
    if action == 'create':
        await provider.provision(ownership, workspace_record['specification'])
    elif action == 'ensure':
        if not await provider.inspect(ownership):
            message = 'Owned runtime is not available'
            raise RuntimeError(message)
    elif action == 'destroy':
        await provider.destroy(ownership, request['payload']['retention'])


class OperationWorker:
    def __init__(self, storage, providers):
        self.storage, self.providers = storage, providers

    async def run_once(self):
        async with self.storage.transaction() as tx:
            pending = [record for record in await tx.all('operations') if record['operation']['status'] in ('pending', 'running')
                       and record['operation']['operation'] in ('workspaces.create', 'workspaces.ensure', 'workspaces.destroy')]
            if not pending:
                return False
            record = min(pending, key=lambda item: (item['operation']['created_at'], item['operation']['operation_id']))
            operation = record['operation']
            operation.update(status='running', updated_at=now())
            await tx.put('operations', operation['operation_id'], record)
            workspace_id = operation['resources']['workspace_id']
            workspace_record = await tx.get('workspace_records', workspace_id)
            ownership = await tx.get('runtime_ownership', workspace_id)
        workspace = workspace_record['workspace']
        action = operation['operation'].split('.')[-1]
        logger.info('operation running', extra={'workspace_id': workspace_id, 'operation_id': operation['operation_id'],
                    'causal_path': record['request']['causal_path']})
        provider = self.providers.get(workspace['provider'])
        failure = None
        try:
            await execute(provider, action, ownership, workspace_record, record['request'])
        except ProviderUnavailable as error:
            logger.exception('provider unavailable', extra={'workspace_id': workspace_id, 'operation_id': operation['operation_id']})
            failure = {'code': 'PROVIDER_ERROR', 'message': str(error), 'retryable': False}
        except Exception:
            logger.exception('provider operation failed', extra={'workspace_id': workspace_id, 'operation_id': operation['operation_id']})
            failure = {'code': 'PROVIDER_ERROR', 'message': f'Provider could not {action} the workspace; inspect VCP logs', 'retryable': False}
        # Cancellation/crash before this transaction leaves the durable running
        # operation to reconcile using the same ownership key on restart.
        async with self.storage.transaction() as tx:
            # Provider calls run outside the transaction. Runtime updates may
            # have changed this workspace in the meantime; merge lifecycle
            # changes into the current document, never the pre-call snapshot.
            workspace_record = await tx.get('workspace_records', workspace_id)
            workspace = workspace_record['workspace']
            timestamp = now()
            operation['updated_at'] = timestamp
            outgoing = None
            if failure:
                operation.update(status='failed', error=failure)
                # A failed ensure does not imply the environment failed. Failed
                # provisioning/destruction retains ownership for explicit cleanup.
                if action != 'ensure':
                    workspace.update(state='failed', failure=failure, version=workspace['version'] + 1, updated_at=timestamp)
                    outgoing = event(workspace, operation, record['request'], 'workspace.failed', {'error': failure})
            else:
                operation['status'] = 'succeeded'
                if action == 'create':
                    runtime = {'runtime_id': ownership['runtime_id'], 'generation': ownership['generation'], 'state': 'running', 'started_at': timestamp}
                    workspace.update(state='ready', runtime=runtime, version=workspace['version'] + 1, updated_at=timestamp)
                    operation['result'] = {'workspace_id': workspace_id, 'runtime_id': runtime['runtime_id'], 'runtime_generation': runtime['generation']}
                    outgoing = event(workspace, operation, record['request'], 'workspace.ready', {'runtime_generation': runtime['generation']})
                elif action == 'ensure':
                    operation['result'] = {'runtime_id': ownership['runtime_id'], 'runtime_generation': ownership['generation'], 'restored': False}
                else:
                    retention = record['request']['payload']['retention']
                    workspace.pop('runtime', None)
                    workspace.pop('failure', None)
                    workspace.update(state='destroyed', version=workspace['version'] + 1, updated_at=timestamp)
                    operation['result'] = {'retention': retention}
                    outgoing = event(workspace, operation, record['request'], 'workspace.destroyed', {'retention': retention})
            if workspace_record.get('active_operation') == operation['operation_id']:
                workspace_record.pop('active_operation')
            await tx.put('workspace_records', workspace_id, workspace_record)
            await tx.put('operations', operation['operation_id'], record)
            if outgoing:
                await tx.put('outbox', outgoing['event_id'], {'event': outgoing, 'published': False})
        return True
