"""Explicit test-only ACP adapter, installed only by bundle --fixture."""
import json
from pathlib import Path
import subprocess
import sys
import uuid

sessions = Path.home() / 'fixture-sessions.json'


def send(value):
    print(json.dumps({'jsonrpc': '2.0', **value}), flush=True)


for line in sys.stdin:
    request = json.loads(line)
    method, params = request.get('method'), request.get('params', {})
    result = {}
    if method == 'initialize':
        result = {'protocolVersion': 1, 'agentCapabilities': {'loadSession': True}}
    elif method in ('session/new', 'session/load'):
        known = json.loads(sessions.read_text()) if sessions.exists() else []
        if method == 'session/new':
            session_id = 'fixture_' + uuid.uuid4().hex
            known.append(session_id)
            sessions.write_text(json.dumps(known))
        else:
            session_id = params['sessionId']
            assert session_id in known
        result = {'sessionId': session_id, 'modes': {'availableModes': [{'id': 'read-only', 'name': 'Read only'}]}}
    elif method == 'session/prompt':
        text = params['prompt'][0]['text']
        if 'WAIT_FOR_CANCEL' in text:
            # A child deliberately ignores the graceful path. Cgroup fencing
            # must remove it before the daemon can report cancellation.
            subprocess.Popen(['/bin/sleep', '600'])
            send({'method': 'session/update', 'params': {'sessionId': params['sessionId'],
                  'update': {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'WAITING'}}}})
            cancellation = json.loads(sys.stdin.readline())
            assert cancellation['method'] == 'session/cancel'
            result = {'stopReason': 'cancelled'}
        else:
            send({'method': 'session/update', 'params': {'sessionId': params['sessionId'],
                  'update': {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'VM_TASK_OK'}}}})
            result = {'stopReason': 'end_turn', 'usage': {'inputTokens': 3, 'outputTokens': 2}}
    send({'id': request['id'], 'result': result})
