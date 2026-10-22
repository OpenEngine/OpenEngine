"""ACP subprocess fixture; requires no model credentials or agent SDK."""
import json
from pathlib import Path
import sys
import time
import uuid

log = Path(sys.argv[1])
mode = sys.argv[2] if len(sys.argv) > 2 else 'normal'
sessions = log.with_suffix('.sessions')


def send(message):
    print(json.dumps({'jsonrpc': '2.0', **message}), flush=True)


def update(session_id, text):
    send({'method': 'session/update', 'params': {'sessionId': session_id,
          'update': {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': text}}}})


for line in sys.stdin:
    request = json.loads(line)
    with log.open('a') as file:
        file.write(json.dumps(request) + '\n')
    method, params = request.get('method'), request.get('params', {})
    result = {}
    if method == 'initialize':
        result = {'protocolVersion': 1, 'agentCapabilities': {'loadSession': mode != 'no_resume'}}
    elif method in ('session/new', 'session/load'):
        known = json.loads(sessions.read_text()) if sessions.exists() else []
        if method == 'session/new':
            session_id = 'acp_' + uuid.uuid4().hex
            known.append(session_id)
            sessions.write_text(json.dumps(known))
        else:
            session_id = params['sessionId']
            if session_id not in known:
                send({'id': request['id'], 'error': {'code': -32602, 'message': 'Unknown session'}})
                continue
            update(session_id, 'Replayed history must not appear as new progress')
        result = {'sessionId': session_id, 'modes': {'availableModes': [{'id': 'read-only', 'name': 'Read only'}]},
                  'configOptions': [{'id': 'model', 'category': 'model'}]}
    elif method == 'session/prompt':
        if mode == 'exit':
            sys.exit(1)
        if mode in ('cancel', 'ignore_cancel'):
            update(params['sessionId'], 'Waiting for cancellation.')
            if mode == 'ignore_cancel':
                time.sleep(60)
            cancellation = json.loads(sys.stdin.readline())
            with log.open('a') as file:
                file.write(json.dumps(cancellation) + '\n')
            assert cancellation['method'] == 'session/cancel'
            assert cancellation['params']['sessionId'] == params['sessionId']
            send({'id': request['id'], 'result': {'stopReason': 'cancelled'}})
            continue
        send({'id': 9999, 'method': 'session/request_permission', 'params': {'sessionId': params['sessionId'], 'options': []}})
        permission = json.loads(sys.stdin.readline())
        assert permission['result']['outcome']['outcome'] == 'cancelled'
        update(params['sessionId'], 'Inspected workspace. ')
        update(params['sessionId'], 'Found the requested result.')
        # Cumulative notifications must not become token deltas.
        send({'method': 'session/update', 'params': {'sessionId': params['sessionId'],
              'update': {'sessionUpdate': 'usage_update', 'used': 5000, 'size': 10000}}})
        result = {'stopReason': 'end_turn', 'usage': {'inputTokens': 10, 'outputTokens': 7}}
    send({'id': request['id'], 'result': result})
    if method == 'session/set_mode' and mode == 'stop_reading':
        time.sleep(60)
