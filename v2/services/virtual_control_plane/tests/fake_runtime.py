"""A ready runtime boundary for acceptance tests; never starts an executor."""
from virtual_control_plane.domain.lifecycle import unsupported


class FakeExecutionContext:
    async def prepare(self, workspace_record, runtime, payload):
        if payload['agent']['runner'] != 'fake':
            unsupported('executor.' + payload['agent']['runner'])
        return {'working_directory': '.', 'instruction_files': [], 'references': []}
