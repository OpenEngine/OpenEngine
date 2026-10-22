"""Prepare execution context from persisted workspace specification through a port."""
from virtual_control_plane.domain.lifecycle import Rejected, unsupported


class RuntimeContext:
    requires_registration = True

    def __init__(self, runners=('codex',)):
        self.runners = runners

    async def prepare(self, workspace_record, runtime, payload):
        runner = payload['agent']['runner']
        if runner not in self.runners:
            unsupported('executor.' + runner)
        if runner == 'codex' and payload['agent'].get('settings'):
            raise Rejected('INVALID_ARGUMENT', 'Codex settings are operator-configured')
        context = workspace_record['specification'].get('context', {})
        return {'working_directory': 'repositories/0' if context.get('repositories') else '.',
                'instruction_files': ['AGENTS.md'] if context.get('instructions') else [], 'references': []}


class FakeRuntimeContext(RuntimeContext):
    def __init__(self):
        super().__init__(('fake',))
