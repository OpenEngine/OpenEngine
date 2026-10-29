"""Authorized session snapshots and scoped pagination with durable cursors."""
from virtual_control_plane.domain.lifecycle import Rejected, identity


def snapshot(record, attempts):
    session = dict(record['session'])
    matching = [item for item in attempts if item['attempt']['session_id'] == session['session_id']]
    matching.sort(key=lambda item: (item['created_at'], item['attempt']['attempt_id']), reverse=True)
    session['attempts'] = [item['attempt'] for item in matching[:20]]
    return session


async def read_sessions(tx, kind, target, params, actor, authorizer, subject):
    if kind == 'session':
        record = await tx.get('sessions', target)
        if record is None:
            raise Rejected('NOT_FOUND', 'Session does not exist')
        await authorizer.authorize(actor, subject, record['owner'])
        return {'session': snapshot(record, await tx.all('attempts'))}
    workspace = await tx.get('workspace_records', target)
    if workspace is None:
        raise Rejected('NOT_FOUND', 'Workspace does not exist')
    await authorizer.authorize(actor, subject, workspace['owner'])
    filters = {'kind': 'sessions', 'workspace_id': target, 'state': sorted(params.get('state', []))}
    after = None
    if params.get('cursor'):
        cursor = await tx.get('cursors', params['cursor'])
        if cursor is None or cursor['owner'] != actor or cursor['filters'] != filters:
            raise Rejected('INVALID_CURSOR', 'Cursor is unknown or filters changed')
        after = tuple(cursor['after'])
    records = []
    for record in await tx.all('sessions'):
        session = record['session']
        if record['owner'] != actor or session['workspace_id'] != target:
            continue
        if filters['state'] and session['state'] not in filters['state']:
            continue
        if after is None or (session['created_at'], session['session_id']) < after:
            records.append(record)
    records.sort(key=lambda item: (item['session']['created_at'], item['session']['session_id']), reverse=True)
    limit = params.get('limit', 50)
    attempts = await tx.all('attempts')
    page = [snapshot(record, attempts) for record in records[:limit]]
    next_cursor = None
    if len(records) > limit:
        next_cursor = identity('cursor')
        await tx.put('cursors', next_cursor, {'owner': actor, 'filters': filters,
                     'after': [page[-1]['created_at'], page[-1]['session_id']]})
    return {'items': page, 'next_cursor': next_cursor}


async def read_history(tx, target, params, actor, authorizer, subject):
    record = await tx.get('sessions', target)
    if record is None:
        raise Rejected('NOT_FOUND', 'Session does not exist')
    await authorizer.authorize(actor, subject, record['owner'])
    filters = {'query': 'session_events', 'session_id': target,
               'kind': sorted(params.get('kind', [])), 'attempt_id': params.get('attempt_id')}
    after = 0
    if params.get('cursor'):
        cursor = await tx.get('cursors', params['cursor'])
        if not cursor or cursor['owner'] != actor or cursor['filters'] != filters:
            raise Rejected('INVALID_CURSOR', 'Cursor is unknown or filters changed')
        after = cursor['after']
    entries = []
    for row in await tx.all('session_history'):
        if row['session_id'] != target or row['sequence'] <= after:
            continue
        entry = {key: value for key, value in row.items() if key != 'session_id'}
        entry.setdefault('kind', 'message')
        if filters['kind'] and entry['kind'] not in filters['kind']:
            continue
        if filters['attempt_id'] and entry.get('attempt_id') != filters['attempt_id']:
            continue
        entries.append(entry)
    entries.sort(key=lambda entry: entry['sequence'])
    limit = params.get('limit', 50)
    page, cursor = entries[:limit], None
    if len(entries) > limit:
        cursor = identity('cursor')
        await tx.put('cursors', cursor, {'owner': actor, 'filters': filters, 'after': page[-1]['sequence']})
    return {'items': page, 'next_cursor': cursor}
