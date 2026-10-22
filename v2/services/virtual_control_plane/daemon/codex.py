"""One allowlisted Codex ACP executor with durable conversation and process identities."""
import asyncio
import json
import os
from pathlib import Path

from virtual_control_plane.daemon.acp import ACPClient, MAX_FRAME
from virtual_control_plane.domain.lifecycle import identity, now
from virtual_control_plane.domain.runtime import invalid


def validate_codex(payload):
    if payload['agent'].get('settings'):
        invalid('Codex executor settings are configured by the operator, not the task')


def instruction_text(root, relative):
    """Open beneath root without following symlinks, including directory races."""
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or '..' in parts:
        invalid('Instruction path must be relative to the workspace')
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(descriptor, 'rb') as file:
            import stat
            if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
                invalid('Instruction must be a regular file')
            data = file.read(131073)
        if len(data) > 131072:
            invalid('Instruction file exceeds context limit')
        return data.decode('utf-8')
    finally:
        os.close(directory)


class CodexBackend:
    progress_kinds = ('conversation', 'usage')
    # ACP v1 has no standard turn-fenced, idempotent active steering RPC.
    # The unpinned extension can fall back to an unowned new turn; never
    # approximate steering with a concurrent prompt or cancel-and-reprompt.
    supports_steering = False

    def __init__(self, executable, fence, *, prompt_timeout=3600):
        # This argv comes only from deployment configuration, never a command.
        self.executable, self.process_fence = tuple(executable), fence
        self.prompt_timeout = prompt_timeout
        self.clients = {}
        self.spawn = asyncio.create_subprocess_exec
        if not self.executable or not Path(self.executable[0]).is_absolute():
            invalid('Codex ACP executable must be an absolute operator-configured path')

    def validate(self, payload):
        validate_codex(payload)

    def plan(self):
        return self.process_fence.plan()

    async def fence(self, handle):
        return await self.process_fence.fence(handle)

    async def cancel(self, attempt_id):
        client = self.clients.get(attempt_id)
        if client and client.session_id:
            await client.send({'jsonrpc': '2.0', 'method': 'session/cancel',
                               'params': {'sessionId': client.session_id}})

    async def run(self, journal, attempt_id):
        async with journal.storage.transaction() as tx:
            attempt = await tx.get('daemon_attempts', attempt_id)
            session_record = await tx.get('daemon_sessions', attempt['command']['target']['session_id'])
        if attempt.get('cancel'):
            return
        command, handle = attempt['command'], attempt['executor_handle']
        payload = command['payload']
        journal.validate_context(command)
        context = payload['context']
        cwd = (journal.root / context['working_directory']).resolve(strict=True)
        previous = session_record.get('executor_session_id') if payload['session_mode'] == 'continue' else None
        if payload['session_mode'] == 'continue' and (not previous or not session_record.get('executor_resumable')):
            invalid('Existing ACP conversation cannot be resumed')
        sections = [f'Workspace instructions ({name}):\n{instruction_text(journal.root, name)}' for name in context['instruction_files']]
        sections.append('Task:\n' + payload['task']['instruction'])
        if payload['task'].get('inputs'):
            sections.append('Task inputs:\n' + json.dumps(payload['task']['inputs'], ensure_ascii=False))
        prompt = '\n\n'.join(sections)
        if len(prompt.encode('utf-8')) > 524288:
            invalid('Assembled task context exceeds limit')
        client = process = None
        summary = ''
        stop_reason = None
        try:
            argv = self.process_fence.command(handle, self.executable)
            process = await self.spawn(*argv, cwd=cwd, stdin=asyncio.subprocess.PIPE,
                      stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=MAX_FRAME + 1)
            client = ACPClient(process)
            self.clients[attempt_id] = client
            session_id, resumable = await client.open_session(cwd, previous=previous, model=payload['agent'].get('model'))
            async with journal.storage.transaction() as tx:
                session_record = await tx.get('daemon_sessions', command['target']['session_id'])
                session_record.update(executor_session_id=session_id, executor_resumable=resumable)
                await tx.put('daemon_sessions', command['target']['session_id'], session_record)
                attempt = await tx.get('daemon_attempts', attempt_id)
                if attempt.get('cancel'):
                    return
                attempt['state'] = 'running'
                attempt['last_event_sequence'] = await journal.append(tx, 'attempt.started', {}, command)
                await tx.put('daemon_attempts', attempt_id, attempt)

            async def update(value):
                nonlocal summary
                if value.get('sessionUpdate') != 'agent_message_chunk':
                    return
                content = value.get('content', {})
                if content.get('type') != 'text' or not isinstance(content.get('text'), str):
                    return
                text = content['text']
                summary = (summary + text)[-4096:]
                for index in range(0, len(text), 32000):
                    await journal.progress(attempt_id, {'kind': 'conversation', 'message_id': identity('msg'),
                                                       'role': 'agent', 'text': text[index:index + 32000]})

            period_start = now()
            result = await client.call('session/prompt', {'sessionId': session_id,
                        'prompt': [{'type': 'text', 'text': prompt}]}, update=update, timeout=self.prompt_timeout)
            stop_reason = result.get('stopReason')
            # Only explicit per-turn token usage is a delta. ACP usage_update
            # notifications report context/cumulative usage and are not added.
            usage = result.get('usage', {})
            if all(type(usage.get(key)) is int and usage[key] >= 0 for key in ('inputTokens', 'outputTokens')):
                await journal.progress(attempt_id, {'kind': 'usage', 'period_start': period_start, 'period_end': now(),
                      'usage': {'input_tokens': usage['inputTokens'], 'output_tokens': usage['outputTokens']}})
        finally:
            self.clients.pop(attempt_id, None)
            if not await self.fence(handle):
                invalid('Executor process tree termination is unconfirmed')
            if client:
                await client.close()
            elif process:
                await process.wait()
        # A prompt result alone is not evidence that tools/processes stopped.
        async with journal.storage.transaction() as tx:
            attempt = await tx.get('daemon_attempts', attempt_id)
            if stop_reason == 'end_turn':
                await journal.finish(tx, attempt, 'completed', {'result': {'summary': summary or 'Agent completed the task.'}})
            else:
                await journal.finish(tx, attempt, 'failed', {'error': {'code': 'PROVIDER_ERROR',
                    'message': 'Agent did not complete its turn', 'retryable': False}, 'reason': 'executor_failed'})
