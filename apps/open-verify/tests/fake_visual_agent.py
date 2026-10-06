"""ACP wire fixture that advertises and records image input."""

import json
import sys
from pathlib import Path

for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if method == 'initialize':
        result = {'protocolVersion': 1, 'agentCapabilities': {'promptCapabilities': {'image': True}}, 'authMethods': []}
    elif method == 'session/new':
        result = {'sessionId': 'visual-fixture'}
    elif method == 'session/prompt':
        Path(sys.argv[1]).write_text(json.dumps(request['params']['prompt']))
        print(json.dumps({'jsonrpc': '2.0', 'method': 'session/update', 'params': {
            'sessionId': 'visual-fixture', 'update': {'sessionUpdate': 'agent_message_chunk',
            'content': {'type': 'text', 'text': json.dumps({'explanation': 'Image received', 'verdict': 'holds'})}}}}), flush=True)
        result = {'stopReason': 'end_turn'}
    elif 'id' not in request:
        continue
    else:
        result = {}
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
