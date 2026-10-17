"""Deterministic guest peer for the unchanged default workflow and MCP bridge."""
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import uuid

session = uuid.uuid4().hex
servers = []
node = os.environ['OE_TEST_NODE']


def send(value):
    print(json.dumps(value), flush=True)


def say(text):
    send({'jsonrpc': '2.0', 'method': 'session/update', 'params': {
        'sessionId': session, 'update': {'sessionUpdate': 'agent_message_chunk',
            'content': {'type': 'text', 'text': text}}}})


for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if method == 'initialize':
        result = {'protocolVersion': 1, 'agentCapabilities': {}}
    elif method == 'session/new':
        servers = request['params']['mcpServers']
        result = {'sessionId': session}
    elif method == 'session/set_config_option':
        result = {'configOptions': []}
    elif method == 'session/prompt':
        assert not {'GITHUB_TOKEN', 'GH_TOKEN', 'GITLAB_TOKEN', 'SSH_AUTH_SOCK'} & os.environ.keys()
        proof = {'vmProof': node, 'kernel': platform.system(), 'cwd': str(Path.cwd()), 'home': str(Path.home())}
        directory = Path(os.environ['CODEX_HOME']) / 'sessions' if 'CODEX_HOME' in os.environ else Path(os.environ['CLAUDE_CONFIG_DIR']) / 'projects'
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'proof.json').write_text(json.dumps(proof))
        say(json.dumps(proof))
        if not servers:
            assert node == 'naming'
            say(json.dumps({'name': 'Sandbox default workflow'}))
        else:
            config = next(server for server in servers if server['name'] == 'workflow')
            shim = subprocess.Popen([config['command'], *config['args']], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            def rpc(method, params):
                shim.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}) + '\n')
                shim.stdin.flush()
                return json.loads(shim.stdout.readline())['result']
            rpc('initialize', {'protocolVersion': '2024-11-05'})
            outputs = {}
            if node == 'implementation':
                Path('README.md').write_text('Implemented inside the VM\n')
                outputs = {'branch': 'local-implementation'}
            elif node.startswith('review-') or node in {'reranker', 'refute'}:
                assert Path('README.md').read_text() == 'Implemented inside the VM\n'
                outputs = {'findings': '[]'}
            elif node == 'impact-analysis':
                outputs = {'impact_level': 'Green', 'impact_rationale': 'Deterministic test change'}
            result = rpc('tools/call', {'name': 'complete_step', 'arguments': {
                'outcome': 'success', 'summary': 'Completed in VM', 'outputs': outputs}})
            assert not result.get('isError'), result
            shim.stdin.close()
            shim.wait(timeout=10)
        result = {'stopReason': 'end_turn'}
    else:
        continue
    send({'jsonrpc': '2.0', 'id': request['id'], 'result': result})
