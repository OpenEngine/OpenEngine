"""Encode internal execute messages against the separately versioned contract."""
import json
from jsonschema import Draft7Validator, FormatChecker

from virtual_control_plane.domain.lifecycle import Rejected
from virtual_control_plane.transport.nats.validation import Contracts


class RuntimeCommands:
    def __init__(self):
        contracts = Contracts()
        path = contracts.root / 'internal/workspace_runtime/v1/messages.schema.json'
        self.validator = Draft7Validator({'$ref': path.resolve().as_uri() + '#/$defs/execute'},
                                         registry=contracts.registry, format_checker=FormatChecker())

    def encode_execute(self, message):
        errors = list(self.validator.iter_errors(message))
        if errors:
            raise Rejected('INVALID_ARGUMENT', 'Invalid runtime execute context')
        context = message['payload']['context']
        for path in [context['working_directory'], *context['instruction_files']]:
            if path.startswith('/') or '..' in path.split('/') or '\\' in path or '\x00' in path:
                raise Rejected('INVALID_ARGUMENT', 'Runtime context paths must stay within the workspace')
        try:
            wire = json.dumps(message, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')
        except (ValueError, UnicodeError, TypeError):
            raise Rejected('INVALID_ARGUMENT', 'Runtime command must be UTF-8 JSON') from None
        if len(wire) > 1024 * 1024:
            raise Rejected('INVALID_ARGUMENT', 'Runtime command exceeds 1 MiB')
        return wire

    def encode_cancel(self, message):
        from virtual_control_plane.transport.nats.runtime_protocol import Protocol
        return Protocol().encode('cancel', message)

    def encode_steer(self, message):
        from virtual_control_plane.transport.nats.runtime_protocol import Protocol
        return Protocol().encode('steer', message)
