"""Small ACP v1 JSON-RPC client. No LangGraph or langgraph-acp dependency."""
import asyncio
import json

from virtual_control_plane.domain.runtime import invalid

MAX_FRAME = 1024 * 1024


class ACPClient:
    def __init__(self, process):
        self.process = process
        self.next_id = 0
        self.session_id = None
        self.write_lock = asyncio.Lock()
        self.stderr = asyncio.create_task(self.drain_stderr())

    async def drain_stderr(self):
        # Drain without persisting arbitrary agent diagnostics/secrets.
        while await self.process.stderr.read(8192):
            pass

    async def send(self, value):
        wire = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8') + b'\n'
        if len(wire) > MAX_FRAME:
            invalid('ACP frame exceeds configured limit')
        async with self.write_lock:
            self.process.stdin.write(wire)
            await self.process.stdin.drain()

    async def call(self, method, params, *, update=None, timeout=30):
        self.next_id += 1
        request_id = self.next_id
        async with asyncio.timeout(timeout):
            # Bound pipe backpressure as well as the response wait. A wedged
            # adapter must not prevent the executor from reaching fencing.
            await self.send({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
            while True:
                line = await self.process.stdout.readline()
                if not line or len(line) > MAX_FRAME:
                    invalid('ACP agent closed or exceeded the frame limit')
                message = json.loads(line)
                if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
                    invalid('Invalid ACP envelope')
                if 'method' in message:
                    if 'id' in message:
                        await self.answer(message)
                    elif message['method'] == 'session/update' and update:
                        data = message.get('params', {})
                        if data.get('sessionId') != self.session_id:
                            invalid('ACP update belongs to another session')
                        await update(data.get('update', {}))
                    continue
                if message.get('id') != request_id:
                    invalid('ACP response identity mismatch')
                if 'error' in message:
                    invalid('ACP agent rejected the request')
                result = message.get('result')
                if not isinstance(result, dict):
                    invalid('ACP response must be an object')
                return result

    async def answer(self, message):
        reply = {'jsonrpc': '2.0', 'id': message['id']}
        if message['method'] == 'session/request_permission':
            # Approvals are deliberately not implemented in this milestone.
            reply['result'] = {'outcome': {'outcome': 'cancelled'}}
        else:
            reply['error'] = {'code': -32601, 'message': 'Client method is not supported'}
        await self.send(reply)

    async def open_session(self, cwd, *, previous=None, model=None):
        initialized = await self.call('initialize', {'protocolVersion': 1, 'clientCapabilities': {
            'fs': {'readTextFile': False, 'writeTextFile': False}, 'terminal': False},
            'clientInfo': {'name': 'openengine-vcp', 'version': '0.0.0'}})
        if initialized.get('protocolVersion') != 1:
            invalid('ACP protocol version mismatch')
        capabilities = initialized.get('agentCapabilities', {})
        params = {'cwd': str(cwd), 'mcpServers': []}
        if previous:
            if not capabilities.get('loadSession'):
                invalid('ACP agent cannot resume the existing session')
            params['sessionId'] = previous
        result = await self.call('session/load' if previous else 'session/new', params)
        self.session_id = previous or result.get('sessionId')
        if not isinstance(self.session_id, str) or not self.session_id or len(self.session_id) > 1024:
            invalid('ACP agent returned an invalid session identity')
        # Codex's advertised read-only mode avoids silently approving tools.
        modes = result.get('modes', {}).get('availableModes', [])
        if not any(mode.get('id') == 'read-only' for mode in modes):
            invalid('Configured Codex adapter must expose read-only mode')
        await self.call('session/set_mode', {'sessionId': self.session_id, 'modeId': 'read-only'})
        if model:
            options = result.get('configOptions', [])
            option = next((item for item in options if item.get('category') == 'model'), None)
            if not option:
                invalid('ACP adapter does not expose model configuration')
            await self.call('session/set_config_option', {'sessionId': self.session_id, 'configId': option['id'], 'value': model})
        return self.session_id, bool(capabilities.get('loadSession'))

    async def close(self):
        # Caller must fence the process tree first, including on cancellation.
        if self.process.stdin:
            self.process.stdin.close()
        async def discard_output():
            # A cancelled prompt reader can leave stdout paused at its buffer
            # limit. Drain it so subprocess.wait() can finish after fencing.
            while await self.process.stdout.read(8192):
                pass

        async with asyncio.timeout(5):
            await asyncio.gather(discard_output(), self.process.wait(), self.stderr)
