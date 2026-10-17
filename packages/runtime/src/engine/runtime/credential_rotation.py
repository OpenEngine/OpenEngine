"""Exclusive Codex refresh ownership and guarded login persistence.

No guest document is trusted as host configuration: only validated token fields
for the same account may replace the selected login's token fields.
"""
import asyncio
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Never

from engine.ports.sandbox_home import SandboxCredentialState
from .model_credentials import ModelCredential, ModelCredentialError, ModelCredentialSource, ModelProvider, _read, explicit_model_credential


def _fingerprint(credential: ModelCredential) -> str:
    tokens = json.loads(credential.value).get('tokens', {}) if credential.provider == 'codex' and credential.kind == 'login' else {}
    # A changed timestamp/access token does not establish fresh refresh
    # ownership. Only a different refresh token clears a crash quarantine.
    value = tokens.get('refresh_token', credential.value)
    return hashlib.sha256(value.encode()).hexdigest()


def _refuse() -> Never:
    message = 'guest model credential state is invalid, changed accounts, or cannot be safely retained'
    raise ModelCredentialError(message)


def _replace(path: Path, value: str) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.oe-auth-', delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            stream.write(value.encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _collect_secrets(values: Mapping[str, str | None]) -> tuple[str, ...]:
    found = []
    for name, value in values.items():
        if value is None:
            continue
        if name.endswith('model-token'):
            found.extend((value, value.strip()))
            continue
        try:
            document = json.loads(value)
        except ValueError:
            _refuse()
        def tokens(item):
            if isinstance(item, dict):
                for key, child in item.items():
                    if key in {'access_token', 'id_token', 'refresh_token', 'accessToken', 'refreshToken', 'OPENAI_API_KEY'} and isinstance(child, str):
                        found.extend((child, child.strip()))
                    else:
                        tokens(child)
        tokens(document)
    return tuple(dict.fromkeys(found))


def _validate_rotation(current: ModelCredential, values: Mapping[str, str | None], old_secrets: set[str]) -> ModelCredential | None:
    if current.provider != 'codex':
        if not values.get('.claude/model-token'):
            _refuse()
        return None
    value = values.get('.codex/auth.json')
    if value is None:
        _refuse()
    assert value is not None
    candidate = ModelCredential('codex', value)
    previous, updated = json.loads(current.home().files['.codex/auth.json']), json.loads(candidate.value)
    if previous.get('OPENAI_API_KEY') != updated.get('OPENAI_API_KEY'):
        _refuse()
    before, after = previous.get('tokens', {}), updated.get('tokens', {})
    if before.get('account_id') != after.get('account_id') or previous.get('auth_mode') != updated.get('auth_mode'):
        _refuse()
    if any(isinstance(token, str) and (token != token.strip() or "\0" in token) for key, token in after.items() if key != 'account_id'):
        _refuse()
    if not before.get('refresh_token') and previous != updated:
        _refuse()
    if before and not all(isinstance(after.get(key), str) and after[key] for key in ('access_token', 'refresh_token', 'id_token') if before.get(key)):
        _refuse()
    if after.get('refresh_token') != before.get('refresh_token') and after.get('refresh_token') in old_secrets:
        _refuse()
    if candidate.value == current.value or (current.kind == 'api-key' and previous == updated):
        return None
    if current.source_path is None:
        # Refreshable anonymous/custom sources need a durable selected
        # login owner; do not pretend a rotated one can be reused safely.
        _refuse()
    return candidate


def _merge_rotation(raw: str | None, current: ModelCredential, candidate: ModelCredential) -> str:
    if raw is None or ModelCredential('codex', raw).value != current.value:
        message = 'selected Codex login changed outside its VM lease; refusing to overwrite it'
        raise ModelCredentialError(message)
    document = json.loads(raw)
    # Preserve host-only metadata, never copy arbitrary guest fields.
    updated = json.loads(candidate.value)
    document['tokens'] = {**document.get('tokens', {}), **updated.get('tokens', {})}
    for key in ('auth_mode', 'last_refresh'):
        if key in updated:
            document[key] = updated[key]
    return json.dumps(document)


class CredentialObserver:
    def __init__(self, credential: ModelCredential, marker: Path | None = None):
        self.current = credential
        self.marker = marker
        self.fingerprints = {_fingerprint(credential)}
        self.state = SandboxCredentialState(
            files=('.codex/auth.json',) if credential.provider == 'codex' else ('.claude/model-token', '.claude/.credentials.json'),
            observe=self.observe, redactions=credential.home().redactions,
        )

    def observe(self, values: Mapping[str, str | None]) -> None:
        old_secrets = set(self.state.redactions)
        # Collection precedes rejection: even invalid account/mode changes
        # contribute their tokens to subsequent leak checks.
        found = _collect_secrets(values)
        self.state.redactions = tuple(dict.fromkeys((*self.state.redactions, *found)))
        candidate = _validate_rotation(self.current, values, old_secrets)
        if candidate is not None:
            self._persist(candidate)

    def _persist(self, candidate: ModelCredential) -> None:
        path = self.current.source_path
        assert path is not None
        document = _merge_rotation(_read(path), self.current, candidate)
        if self.marker is not None:
            # Record both sides before replacing auth.json: a crash on either
            # side of that replacement must never look like a fresh host login.
            self.fingerprints.add(_fingerprint(candidate))
            _replace(self.marker, json.dumps(sorted(self.fingerprints)))
        _replace(path, document)
        self.current = replace(candidate, source_path=path)


@asynccontextmanager
async def credential_lease(source: ModelCredentialSource | None, provider: ModelProvider, values: Mapping[str, str]):
    async def resolve():
        return await asyncio.to_thread(source, provider, values) if source else explicit_model_credential(provider, values)
    credential = await resolve()
    if credential is None:
        yield None, None
        return
    if credential.provider != provider:
        message = 'credential source returned a different model provider'
        raise ModelCredentialError(message)
    profile = json.loads(credential.value) if provider == 'codex' and credential.kind == 'login' else {}
    if not profile.get('tokens', {}).get('refresh_token'):
        observer = CredentialObserver(credential)
        yield credential, observer.state
        return
    if credential.source_path is None or not profile.get('tokens', {}).get('account_id'):
        message = 'refreshable Codex VM logins require a selected credential file for exclusive refresh ownership'
        raise ModelCredentialError(message)
    import fcntl
    path = credential.source_path
    if path.is_symlink():
        _refuse()
    # Stable lock inode shared across runs, runtimes and Engine processes. Never
    # unlink it: waiters may already have opened the inode.
    lock = path.with_name('.openengine-refresh.lock')
    active = path.with_name('.openengine-refresh.active')
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)
        credential = await resolve()
        if credential is None or credential.source_path != path:
            _refuse()
        if active.exists() or active.is_symlink():
            try:
                markers = json.loads(active.read_text()) if not active.is_symlink() and active.stat().st_size <= 16384 else None
                import re
                valid = isinstance(markers, list) and 0 < len(markers) <= 128 and all(
                    isinstance(item, str) and re.fullmatch('[0-9a-f]{64}', item) for item in markers)
            except (ValueError, OSError):
                valid, markers = False, None
            if not valid or not isinstance(markers, list) or _fingerprint(credential) in markers:
                message = 'previous Codex VM lost refresh ownership before saving credentials; sign in again before reusing this login'
                raise ModelCredentialError(message)
        observer = CredentialObserver(credential, active)
        _replace(active, json.dumps(sorted(observer.fingerprints)))
        try:
            yield credential, observer.state
        finally:
            if observer.state.finalized or not observer.state.exposed:
                active.unlink(missing_ok=True)
            else:
                _replace(active, json.dumps(sorted(observer.fingerprints)))
    finally:
        os.close(fd)
