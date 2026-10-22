"""Subprocess fixture: real VCP with a durable, controllable compute adapter."""
import asyncio
from pathlib import Path
import os
from virtual_control_plane.providers.smolvm.provider import CAPABILITIES
from virtual_control_plane.service import parser, serve
from virtual_control_plane.tests.fake_runtime import FakeExecutionContext


class FakeCompute:
    capabilities = {**CAPABILITIES, 'provider': 'fake'}
    def __init__(self, directory, fail=False, crash=False):
        self.directory, self.fail, self.crash = directory, fail, crash
        directory.mkdir(parents=True, exist_ok=True)
    def validate(self, specification):
        pass
    async def provision(self, ownership, specification):
        if self.fail:
            message = 'Injected provider failure'
            raise RuntimeError(message)
        (self.directory / ownership['runtime_id']).touch()
        marker = self.directory / 'crashed'
        if self.crash and not marker.exists():
            marker.touch()
            os._exit(73)
    async def inspect(self, ownership):
        return (self.directory / ownership['runtime_id']).exists()
    async def destroy(self, ownership, retention):
        (self.directory / ownership['runtime_id']).unlink(missing_ok=True)


if __name__ == '__main__':
    arguments_parser = parser()
    arguments_parser.add_argument('--fake-state', type=Path, required=True)
    arguments_parser.add_argument('--fail', action='store_true')
    arguments_parser.add_argument('--crash', action='store_true')
    arguments = arguments_parser.parse_args()
    asyncio.run(serve(arguments, {'fake': FakeCompute(arguments.fake_state, arguments.fail, arguments.crash)},
                      FakeExecutionContext()))
