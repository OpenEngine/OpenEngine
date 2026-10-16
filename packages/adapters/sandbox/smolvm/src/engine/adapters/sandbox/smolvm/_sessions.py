"""Validate guest session checkpoints before atomically replacing host state."""
import os
from pathlib import Path

from . import _guest


def validate(directory: Path, provider: str, redactions: tuple[str, ...] = ()) -> None:
    if not directory.is_dir() or directory.is_symlink():
        message = 'agent session checkpoint must be a directory'
        raise ValueError(message)
    expected = 'sessions' if provider == 'codex' else 'projects'
    needles = tuple(secret.encode() for secret in redactions if secret)
    for path in directory.rglob('*'):
        if any(secret in os.fsencode(path.relative_to(directory)) for secret in needles):
            message = 'agent session checkpoint contains an injected credential'
            raise ValueError(message)
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            message = 'agent session checkpoints may contain only regular files and directories'
            raise ValueError(message)
        if path.relative_to(directory).parts[0] != expected or (path.name == expected and path.parent == directory and not path.is_dir()):
            message = 'agent session checkpoint contains an unexpected directory'
            raise ValueError(message)
        if path.is_file():
            content = path.read_bytes()
            if any(secret in content for secret in needles):
                message = 'agent session checkpoint contains an injected credential'
                raise ValueError(message)
            path.chmod(0o600)
        else:
            path.chmod(0o700)


def checkpoint(archive: Path, temporary: Path, destination: Path, provider: str, redactions: tuple[str, ...]) -> None:
    extracted = temporary / 'validated'
    _guest.unpack(archive, extracted, True)
    validate(extracted, provider, redactions)
    # Repack only validated files, never retain an untrusted guest tar verbatim.
    staged = temporary / 'state.tar'
    _guest.pack(extracted, staged)
    staged.chmod(0o600)
    os.replace(staged, destination)
