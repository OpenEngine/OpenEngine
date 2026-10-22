"""Internal runtime identities and errors, independent of transport."""
import hashlib
import json

BINDING = ('workspace_id', 'runtime_id', 'runtime_generation')


def binding(value):
    return {key: value[key] for key in BINDING}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


def error(code, message):
    return {'code': code, 'message': message, 'retryable': code in ('UNAVAILABLE', 'EVENT_GAP')}


def invalid(message):
    raise ValueError(message)
