"""Authorized snapshots and filter-bound durable keyset cursors."""
from virtual_control_plane.application.queries.sessions import read_sessions, read_history
from virtual_control_plane.domain.lifecycle import Rejected, identity


class Queries:
    def __init__(self, storage, providers, authorizer):
        self.storage, self.providers, self.authorizer = storage, providers, authorizer

    async def get(self, subject, kind, target, request, actor):
        reply = {key: request[key] for key in ('request_id',) if key in request}
        try:
            result = await self._get(subject, kind, target, request, actor)
        except Rejected as error:
            return {**reply, 'status': 'error', 'error': error.error}
        return {**reply, 'status': 'ok', 'result': result}

    async def _get(self, subject, kind, target, request, actor):
        await self.authorizer.authorize(actor, subject, None)
        params = request.get('params', {})
        async with self.storage.transaction() as tx:
            if kind == 'capabilities':
                result = {'providers': [provider.capabilities for name, provider in sorted(self.providers.items())
                                        if params.get('provider', name) == name]}
            elif kind == 'session_events':
                result = await read_history(tx, target, params, actor, self.authorizer, subject)
            elif kind in ('session', 'sessions'):
                result = await read_sessions(tx, kind, target, params, actor, self.authorizer, subject)
            elif kind in ('workspace', 'operation'):
                collection = 'workspace_records' if kind == 'workspace' else 'operations'
                record = await tx.get(collection, target)
                if record is None:
                    raise Rejected('NOT_FOUND', f'{kind} does not exist')
                await self.authorizer.authorize(actor, subject, record['owner'])
                result = {kind: record[kind]}
            elif kind == 'workspaces':
                filters = {key: value for key, value in params.items() if key not in ('cursor', 'limit')}
                if 'state' in filters:
                    filters['state'] = sorted(filters['state'])
                after = None
                if params.get('cursor'):
                    cursor = await tx.get('cursors', params['cursor'])
                    if cursor is None or cursor['owner'] != actor or cursor['filters'] != filters:
                        raise Rejected('INVALID_CURSOR', 'Cursor is unknown or filters changed')
                    after = tuple(cursor['after'])
                items = []
                for record in await tx.all('workspace_records'):
                    if record['owner'] != actor:
                        continue
                    workspace = record['workspace']
                    states = filters.get('state')
                    if states and workspace['state'] not in states:
                        continue
                    if not states and workspace['state'] == 'destroyed':
                        continue
                    if 'provider' in filters and workspace['provider'] != filters['provider']:
                        continue
                    if 'job_id' in filters and workspace['labels'].get('job_id') != filters['job_id']:
                        continue
                    key = (workspace['created_at'], workspace['workspace_id'])
                    if after is None or key < after:
                        items.append(workspace)
                items.sort(key=lambda item: (item['created_at'], item['workspace_id']), reverse=True)
                limit = params.get('limit', 50)
                page, next_cursor = items[:limit], None
                if len(items) > limit:
                    next_cursor = identity('cursor')
                    await tx.put('cursors', next_cursor, {'owner': actor, 'filters': filters,
                                 'after': [page[-1]['created_at'], page[-1]['workspace_id']]})
                result = {'items': page, 'next_cursor': next_cursor}
            else:
                raise Rejected('UNSUPPORTED_CAPABILITY', 'Query not implemented', capability=kind)
        return result
