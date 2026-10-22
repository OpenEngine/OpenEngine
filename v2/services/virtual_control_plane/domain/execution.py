"""Session and attempt state machines; snapshots use public contract fields."""
from copy import deepcopy

from virtual_control_plane.domain.lifecycle import Rejected, identity

SESSION_TRANSITIONS = {'idle': {'active', 'closed'}, 'active': {'idle', 'closed'}, 'closed': set()}
ATTEMPT_TRANSITIONS = {
    'starting': {'running', 'failed', 'cancelling', 'cancelled'},
    'running': {'completed', 'failed', 'cancelling'},
    'cancelling': {'completed', 'failed', 'cancelled'},
    'completed': set(), 'failed': set(), 'cancelled': set(),
}


def transition(model, state, *, timestamp):
    """Return a new snapshot, rejecting transitions out of terminal states."""
    kind = 'attempt' if 'attempt_id' in model else 'session'
    transitions = ATTEMPT_TRANSITIONS if kind == 'attempt' else SESSION_TRANSITIONS
    if state not in transitions[model['state']]:
        raise Rejected('INVALID_STATE', f'Cannot transition {kind} to {state}', resource_type=kind,
                       resource_id=model[f'{kind}_id'], current_state=model['state'],
                       allowed_states=sorted(transitions[model['state']]))
    result = deepcopy(model)
    result['state'] = state
    if kind == 'session':
        result['updated_at'] = timestamp
    elif state == 'running':
        result['started_at'] = timestamp
    elif state in ('completed', 'failed', 'cancelled'):
        result['ended_at'] = timestamp
    return result


def new_session(workspace_id, generation, agent, timestamp):
    return {'session_id': identity('session'), 'workspace_id': workspace_id,
            'runtime_generation': generation, 'agent': deepcopy(agent), 'state': 'idle',
            'attempts': [], 'created_at': timestamp, 'updated_at': timestamp}


def new_attempt(session, instruction):
    return {'attempt_id': identity('attempt'), 'session_id': session['session_id'],
            'workspace_id': session['workspace_id'], 'runtime_generation': session['runtime_generation'],
            'state': 'starting', 'instruction': instruction}
