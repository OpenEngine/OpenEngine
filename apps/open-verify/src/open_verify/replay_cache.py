"""Versioned local action recordings; assertion outcomes are never cached."""

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from open_verify import __version__
from open_verify.contracts import Contract
from open_verify.journey_spec import StepAction

CACHE_VERSION = 1
MAX_CACHE_BYTES = 2_000_000
REPLAY_TOOLS = frozenset({'browser_open', 'browser_snapshot', 'browser_click',
                          'browser_press', 'browser_reload'})


def digest(value) -> str:
    """Hash canonical JSON without keeping raw observations on disk."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def screen_hash(observation: dict) -> str | None:
    """Only complete semantic observations can guard a cached action."""
    if (observation.get('truncated', False) or observation.get('nodes_truncated', False) or not isinstance(observation.get('url'), str)
            or not isinstance(observation.get('snapshot'), str)):
        return None
    return digest({'url': observation['url'], 'snapshot': observation['snapshot'],
                   **({'semantics': observation['semantic_fingerprint']} if 'semantic_fingerprint' in observation else {})})


class RecordedAction(Contract):
    action: StepAction
    before: str = Field(pattern=r'^[a-f0-9]{64}$')
    after: str = Field(pattern=r'^[a-f0-9]{64}$')

    @model_validator(mode='after')
    def supported_action(self):
        if self.action.tool not in REPLAY_TOOLS:
            raise ValueError('Action is excluded from persistent replay')
        return self


class RecordedStep(Contract):
    index: int = Field(ge=0, lt=20)
    initial: str = Field(pattern=r'^[a-f0-9]{64}$')
    final: str = Field(pattern=r'^[a-f0-9]{64}$')
    actions: list[RecordedAction] = Field(max_length=50)


class Recording(Contract):
    version: Literal[1] = CACHE_VERSION
    key: str = Field(pattern=r'^[a-f0-9]{64}$')
    steps: list[RecordedStep] = Field(min_length=1, max_length=20)

    @model_validator(mode='after')
    def unique_steps(self):
        if len({s.index for s in self.steps}) != len(self.steps):
            raise ValueError('Duplicate recorded step index')
        return self


class ReplayStopped(Exception):
    """A cache mismatch or missing prerequisite with a host-assigned outcome code."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


class ReplayCache:
    """Bounded, atomically replaced recordings isolated by canonical project path."""

    def __init__(self, directory: Path, project: Path):
        self.namespace = digest(str(project.resolve()))
        self.directory = directory / self.namespace

    def path(self, key: str) -> Path:
        """Only host-generated digest filenames may address storage."""
        if len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
            raise ValueError('Invalid cache key')
        return self.directory / f'{key}.json'

    def read(self, key: str) -> Recording | None:
        """Reject links, oversized files and unknown schemas before replay."""
        path = self.path(key)
        if path.is_symlink():
            raise ValueError("Recording must not be a symlink")
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        except FileNotFoundError:
            return None
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('Recording is not a regular file')
            raw = stream.read(MAX_CACHE_BYTES + 1)
        if len(raw) > MAX_CACHE_BYTES:
            raise ValueError('Recording exceeds the cache size limit')
        recording = Recording.model_validate_json(raw)
        if recording.key != key:
            raise ValueError('Recording key mismatch')
        return recording

    def write(self, recording: Recording):
        """Readers see a complete private file, even if another writer is interrupted."""
        raw = recording.model_dump_json().encode()
        if len(raw) > MAX_CACHE_BYTES:
            raise ValueError('Recording exceeds the cache size limit')
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.recording-', dir=self.directory)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
            os.replace(temporary, self.path(recording.key))
        finally:
            Path(temporary).unlink(missing_ok=True)

    def invalidate(self, key: str):
        """Remove a stale recording without following its contents or any symlink."""
        self.path(key).unlink(missing_ok=True)


class ReplaySession:
    """One case's cache transaction; commit only after fresh assertions and cleanup pass."""

    def __init__(self, cache, mode, case, engine, artifacts, progress):
        if mode not in {'auto', 'strict', 'refresh', 'off'}:
            raise ValueError('Unknown replay cache mode')
        self.cache, self.mode = cache, mode if cache is not None else 'off'
        self.case, self.engine = case, engine
        self.artifacts, self.progress = artifacts, progress
        self.key = None
        self.recording = None
        self.pending = {}
        self.events = []
        self.step_info = {}
        self.reason = ''
        self.eligible = True
        self.act_indexes = [i for i, s in enumerate(case.journey.steps) if s.kind == 'act']

    def event(self, status, detail, index=None):
        """Record diagnostics without copying cached arguments or observations into prompts."""
        event = {'case_id': self.case.id, 'step': index, 'status': status, 'detail': detail, 'key': self.key}
        self.events.append(event)
        self.artifacts.record('replay_cache', {}, event, status not in {'stale', 'write_error'})
        self.progress(f"  Cache: {status} — {detail}")
        if index is not None:
            self.step_info[index] = {'cache': status, 'cache_detail': detail}

    def prepare(self):
        """Fix cache identity from the accepted plan and engine's replay compatibility contract."""
        if self.mode == 'off' or not self.act_indexes:
            return
        identity = getattr(self.engine, 'replay_identity', lambda: None)()
        if self.case.journey.authenticated or identity is None:
            self.reason = 'Authenticated journeys are not cached' if self.case.journey.authenticated else 'Engine has no replay compatibility identity'
            self.eligible = False
            return
        self.key = digest({'version': CACHE_VERSION, 'package': __version__,
            'project': self.cache.namespace, 'engine': identity,
            'journey': self.case.journey.model_dump(), 'checks': self.case.checks,
            'prerequisites': self.case.prerequisites,
            'tools': {k: v for k, v in self.engine.catalog('execute').items() if k in REPLAY_TOOLS}})
        if self.mode == 'refresh':
            return
        try:
            self.recording = self.cache.read(self.key)
            if self.recording is not None and {s.index for s in self.recording.steps} != set(self.act_indexes):
                raise ValueError('Recording does not cover the accepted action steps')
        except (ValueError, OSError):
            self.reason = 'Recording is malformed, incompatible or unreadable'
            self.invalidate(self.reason)

    def invalidate(self, reason):
        """Strict mode leaves storage untouched; automatic mode drops stale entries."""
        self.recording = None
        self.reason = reason
        self.event('invalidated', reason)
        if self.key and self.mode in {'auto', 'refresh'}:
            try:
                self.cache.invalidate(self.key)
            except OSError:
                self.event('write_error', 'Could not remove stale cache file')

    def lookup(self, index, observation):
        """Only a complete matching initial screen permits action replay."""
        if self.mode == 'off':
            return None
        if not self.eligible or screen_hash(observation) is None:
            self.eligible = False
            reason = self.reason or 'Observation is incomplete; persistent replay is unavailable'
            self.event('bypass', reason, index)
            if self.mode == 'strict':
                raise ReplayStopped('REPLAY_UNAVAILABLE', reason)
            return None
        if self.recording is None:
            status = 'refresh' if self.mode == 'refresh' else 'stale' if self.reason else 'miss'
            reason = self.reason or ('Live recording requested' if status == 'refresh' else 'No compatible recording')
            self.event(status, reason, index)
            if self.mode == 'strict':
                raise ReplayStopped('REPLAY_STALE' if self.reason else 'REPLAY_MISS', reason)
            return None
        step = next(s for s in self.recording.steps if s.index == index)
        if len(step.actions) > self.case.journey.steps[index].max_actions:
            self.stale(index, 'Recording exceeds the current action budget', started=False)
            return None
        if step.initial != screen_hash(observation):
            self.stale(index, 'Initial screen differs from the recording', started=False)
            return None
        self.event('hit', 'Replaying checked actions; assertions will run again', index)
        return step

    def stale(self, index, reason, *, started):
        """Never let fallback repeat operations after replay dispatch has begun."""
        self.invalidate(reason)
        self.event('stale', reason, index)
        if started or self.mode == 'strict':
            raise ReplayStopped('REPLAY_STALE', reason + '; replay stopped without repeating actions')

    def exclude(self, index, reason):
        """Keep potentially sensitive or incomplete traces out of persistent storage."""
        if self.mode != 'off':
            self.eligible = False
            self.event('bypass', reason, index)

    def commit(self, status):
        """Cache complete successful cases only; a fresh assertion failure invalidates a hit."""
        if self.mode == 'off' or not self.act_indexes:
            return
        if status != 'passed':
            if self.recording is not None or (self.mode == 'refresh' and self.key):
                self.invalidate('Journey did not pass fresh verification or cleanup')
            return
        if self.mode == 'strict' or not self.eligible or not self.key:
            return
        if set(self.pending) != set(self.act_indexes):
            return
        if self.recording is not None and self.recording.steps == [self.pending[i] for i in self.act_indexes]:
            return
        try:
            self.cache.write(Recording(key=self.key, steps=[self.pending[i] for i in self.act_indexes]))
            self.event('stored', 'Verified action recording saved')
        except (ValueError, OSError):
            self.event('write_error', 'Recording could not be saved; execution result is unchanged')
