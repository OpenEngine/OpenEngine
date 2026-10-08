"""Action reuse must preserve fresh verification and never repeat partial effects."""

import asyncio
import json
from pathlib import Path

import pytest
from test_journeys import Actor, FakeEngine, case, complete, read_steps
from test_tools import web_app as web_app

from open_verify.artifacts import Artifacts
from open_verify.cli import parser
from open_verify.journey import JourneyRunner
from open_verify.journey_spec import ActDecision
from open_verify.replay_cache import MAX_CACHE_BYTES, ReplayCache
from open_verify.tools import LocalTools


class Engine(FakeEngine):
    def __init__(self, artifacts, *, initial='Cart: 0', after='Cart: 1', failed=False,
                 assertion='passed', truncated=False, revision=1):
        super().__init__(artifacts)
        self.screen = initial
        self.after = after
        self.failed = failed
        self.assertion = assertion
        self.truncated = truncated
        self.revision = revision

    def replay_identity(self):
        return {'engine': 'fixture', 'revision': self.revision}

    def catalog(self, stage):
        return {**super().catalog(stage), 'browser_fill': {}}

    async def execute(self, tool, arguments, **kwargs):
        self.calls.append(tool)
        ok = True
        if tool in {'browser_click', 'browser_fill'}:
            self.screen = self.after
            ok = not self.failed
        return self.artifacts.record(tool, arguments,
            {'url': 'http://localhost/cart', 'snapshot': self.screen, 'truncated': self.truncated}, ok)

    async def assert_check(self, check):
        return self.artifacts.record('assert_check', {},
            {'status': self.assertion, 'detail': 'Fresh assertion'}, self.assertion == 'passed')


class NoActor(Actor):
    async def begin(self):
        pytest.fail('Cache hit must not start an acting session')

    async def act(self, *args, **kwargs):
        pytest.fail('Cache hit must not call the acting model')


def execute(tmp_path, cache, *, actor=None, mode='auto', target=None, **options):
    artifacts = Artifacts(tmp_path / 'runs')
    engine = Engine(artifacts, **options)
    runner = JourneyRunner(engine, artifacts, actor or Actor(), replay_cache=cache,
                           cache_mode=mode, progress=lambda _: None)
    result = asyncio.run(runner.run(target or case('http://localhost')))
    return result, engine, artifacts


def entries(cache):
    return list(cache.directory.glob('*.json'))


def test_verified_recording_replays_actions_and_runs_fresh_assertions(tmp_path):
    cache = ReplayCache(tmp_path / 'cache', tmp_path)
    first, _, initial = execute(tmp_path, cache)
    second, engine, replayed = execute(tmp_path, cache, actor=NoActor())
    assert first.status == second.status == 'passed'
    assert read_steps(initial)[0]['cache'] == 'miss'
    assert read_steps(replayed)[0]['cache'] == 'hit'
    assert read_steps(replayed)[0]['model_calls'] == 1
    assert read_steps(replayed)[0]['actions'] == 1
    assert engine.calls.count('browser_click') == 1
    assert any(e['tool'] == 'assert_check' for e in replayed.observations)
    raw = entries(cache)[0].read_text()
    assert 'Cart: 1' not in raw and 'Cart: 0' not in raw
    assert 'The item was added' not in raw
    assert entries(cache)[0].stat().st_mode & 0o777 == 0o600


def test_real_browser_replays_without_actor_but_keeps_semantic_judge(tmp_path, web_app):
    cache = ReplayCache(tmp_path / 'cache', tmp_path)
    async def run(actor):
        artifacts = Artifacts(tmp_path / 'runs')
        result = await JourneyRunner(LocalTools(tmp_path, artifacts, headless=True), artifacts,
            actor, replay_cache=cache, progress=lambda _: None).run(case(web_app, semantic=True))
        return result, artifacts
    actor = Actor()
    first, _ = asyncio.run(run(actor))
    judge = NoActor()
    second, artifacts = asyncio.run(run(judge))
    assert first.status == second.status == 'passed'
    assert len(actor.judgments) == len(judge.judgments) == 1
    assert 'Cart: 1' in judge.judgments[0][1]['snapshot']
    assert read_steps(artifacts)[0]['model_calls'] == 1
    assert read_steps(artifacts)[1]['model_calls'] == 1
    assert 'raise RuntimeError' in (artifacts.path / second.test_file).read_text()


def test_initial_staleness_falls_back_before_any_cached_action(tmp_path):
    cache = ReplayCache(tmp_path / 'cache', tmp_path)
    execute(tmp_path, cache)
    actor = Actor()
    result, engine, artifacts = execute(tmp_path, cache, actor=actor, initial='Updated cart')
    assert result.status == 'passed'
    assert actor.sessions == 1
    assert engine.calls.count('browser_click') == 1
    assert read_steps(artifacts)[0]['cache'] == 'stale'
    assert any(e['result'].get('status') == 'invalidated' for e in artifacts.observations)
    again, _, replayed = execute(tmp_path, cache, actor=NoActor(), initial='Updated cart')
    assert again.status == 'passed' and read_steps(replayed)[0]['cache'] == 'hit'


@pytest.mark.parametrize('failed', [False, True])
def test_after_dispatch_staleness_blocks_without_live_retry(tmp_path, failed):
    cache = ReplayCache(tmp_path / 'cache', tmp_path)
    execute(tmp_path, cache)
    result, engine, artifacts = execute(tmp_path, cache, actor=NoActor(),
                                       after='Unexpected cart', failed=failed)
    assert result.status == 'blocked'
    assert engine.calls.count('browser_click') == 1
    assert read_steps(artifacts)[0]['code'] == 'REPLAY_STALE'
    assert read_steps(artifacts)[1]['code'] == 'NOT_RUN'
    assert not entries(cache)


def test_fresh_assertion_failure_invalidates_hit_without_retry(tmp_path):
    cache = ReplayCache(tmp_path / 'cache', tmp_path)
    execute(tmp_path, cache)
    result, engine, artifacts = execute(tmp_path, cache, actor=NoActor(), assertion='failed')
    assert result.status == 'failed'
    assert read_steps(artifacts)[0]['cache'] == 'hit'
    assert engine.calls.count('browser_click') == 1
    assert not entries(cache)


@pytest.mark.parametrize('situation', ['missing', 'stale', 'invalid'])
def test_strict_mode_never_falls_back_or_mutates_storage(tmp_path, situation):
    cache = ReplayCache(tmp_path / 'cache', tmp_path)
    if situation != 'missing':
        execute(tmp_path, cache)
    if situation == 'invalid':
        entries(cache)[0].write_text('{broken')
    before = {p.name: p.read_bytes() for p in entries(cache)}
    result, engine, artifacts = execute(tmp_path, cache, actor=NoActor(), mode='strict',
        initial='Changed' if situation == 'stale' else 'Cart: 0')
    assert result.status == 'blocked'
    assert read_steps(artifacts)[0]['code'] == ('REPLAY_MISS' if situation == 'missing' else 'REPLAY_STALE')
    assert 'browser_click' not in engine.calls
    assert before == {p.name: p.read_bytes() for p in entries(cache)}


def test_refresh_and_off_force_live_execution(tmp_path):
    cache = ReplayCache(tmp_path / 'cache', tmp_path)
    execute(tmp_path, cache)
    raw = entries(cache)[0].read_bytes()
    for mode in ('refresh', 'off'):
        actor = Actor()
        result, _, artifacts = execute(tmp_path, cache, actor=actor, mode=mode)
        assert result.status == 'passed' and actor.sessions == 1
        assert read_steps(artifacts)[0]['cache'] == mode
    assert entries(cache)[0].read_bytes() == raw
    unused = ReplayCache(tmp_path / 'unused', tmp_path)
    execute(tmp_path, unused, mode='off')
    assert not unused.directory.exists()


@pytest.mark.parametrize('variant', ['goal', 'assertion', 'url', 'budget', 'engine', 'project'])
def test_identity_change_is_a_miss(tmp_path, variant):
    cache = ReplayCache(tmp_path / 'cache', tmp_path)
    execute(tmp_path, cache)
    target = case('http://localhost')
    options = {}
    if variant == 'goal':
        target.journey.steps[0].instruction = 'Add a different item'
    elif variant == 'assertion':
        target.journey.steps[1].check.text = 'Cart: 2'
    elif variant == 'url':
        target.journey.url = 'http://localhost/other'
    elif variant == 'budget':
        target.journey.steps[0].max_actions = 2
    elif variant == 'engine':
        options['revision'] = 2
    else:
        cache = ReplayCache(tmp_path / 'cache', tmp_path / 'other-project')
    actor = Actor()
    result, _, artifacts = execute(tmp_path, cache, actor=actor, target=target, **options)
    assert result.status == 'passed' and actor.sessions == 1
    assert read_steps(artifacts)[0]['cache'] == 'miss'


@pytest.mark.parametrize('problem', ['malformed', 'oversized', 'schema', 'wrong-key', 'missing-step'])
def test_corrupt_recording_is_replaced_only_after_fresh_verification(tmp_path, problem):
    cache = ReplayCache(tmp_path / 'cache', tmp_path)
    execute(tmp_path, cache)
    path = entries(cache)[0]
    data = json.loads(path.read_text())
    if problem == 'malformed':
        path.write_text('{')
    elif problem == 'oversized':
        path.write_bytes(b' ' * (MAX_CACHE_BYTES + 1))
    else:
        if problem == 'schema':
            data['version'] = 900
        elif problem == 'wrong-key':
            data['key'] = '0' * 64
        else:
            data['steps'][0]['index'] = 8
        path.write_text(json.dumps(data))
    result, _, artifacts = execute(tmp_path, cache)
    assert result.status == 'passed'
    assert read_steps(artifacts)[0]['cache'] == 'stale'
    assert cache.read(path.stem) is not None


@pytest.mark.parametrize('reason', ['fill', 'truncated', 'authenticated', 'failed-assertion'])
def test_ineligible_journeys_do_not_persist_recordings(tmp_path, reason):
    cache = ReplayCache(tmp_path / 'cache', tmp_path)
    actor = Actor()
    target = case('http://localhost')
    options = {}
    if reason == 'fill':
        actor = Actor([ActDecision.model_validate({'kind': 'action', 'action': {
            'tool': 'browser_fill', 'arguments': {'by': 'label', 'name': 'Password', 'value': 'DO-NOT-CACHE'},
            'reason': 'Fill'}}), complete()])
    elif reason == 'truncated':
        options['truncated'] = True
    elif reason == 'authenticated':
        target.journey.authenticated = True
    else:
        options['assertion'] = 'failed'
    result, _, _ = execute(tmp_path, cache, actor=actor, target=target, **options)
    assert result.status == ('failed' if reason == 'failed-assertion' else 'blocked' if reason == 'truncated' else 'passed')
    assert not entries(cache)


def test_cancelled_replay_invalidates_and_preserves_step_cache_status(tmp_path):
    cache = ReplayCache(tmp_path / 'cache', tmp_path)
    execute(tmp_path, cache)
    artifacts = Artifacts(tmp_path / 'runs')
    class Interrupted(Engine):
        async def execute(self, tool, arguments, **kwargs):
            if tool == 'browser_click':
                raise asyncio.CancelledError()
            return await super().execute(tool, arguments, **kwargs)
    runner = JourneyRunner(Interrupted(artifacts), artifacts, NoActor(), replay_cache=cache,
                           progress=lambda _: None)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runner.run(case('http://localhost')))
    assert not entries(cache)
    assert read_steps(artifacts)[0]['cache'] == 'hit'
    assert read_steps(artifacts)[0]['code'] == 'STEP_INTERRUPTED'


def test_cli_exposes_cache_controls_without_changing_existing_defaults():
    assert parser().parse_args(['cart']).cache == 'auto'
    args = parser().parse_args(['cart', '--cache', 'strict', '--cache-dir', '/tmp/recordings'])
    assert args.cache == 'strict' and args.cache_dir == Path('/tmp/recordings')


def test_screen_change_before_second_action_never_repeats_first(tmp_path):
    cache = ReplayCache(tmp_path / 'cache', tmp_path)
    click = ActDecision.model_validate({'kind': 'action', 'action': {
        'tool': 'browser_click', 'arguments': {'by': 'text', 'name': 'Add item'}, 'reason': 'Add'}})
    execute(tmp_path, cache, actor=Actor([click, click, complete()]))
    artifacts = Artifacts(tmp_path / 'runs')
    class Changed(Engine):
        async def execute(self, tool, arguments, **kwargs):
            if tool == 'browser_snapshot' and self.calls.count('browser_click') == 1:
                self.screen = 'Changed between actions'
            return await super().execute(tool, arguments, **kwargs)
    engine = Changed(artifacts)
    result = asyncio.run(JourneyRunner(engine, artifacts, NoActor(), replay_cache=cache,
        progress=lambda _: None).run(case('http://localhost')))
    assert result.status == 'blocked'
    assert engine.calls.count('browser_click') == 1
    assert read_steps(artifacts)[0]['code'] == 'REPLAY_STALE'


def test_screen_change_immediately_before_first_action_can_fall_back(tmp_path):
    cache = ReplayCache(tmp_path / 'cache', tmp_path)
    execute(tmp_path, cache)
    artifacts = Artifacts(tmp_path / 'runs')
    class Changed(Engine):
        async def execute(self, tool, arguments, **kwargs):
            if tool == 'browser_snapshot' and self.calls.count('browser_snapshot') == 1:
                self.screen = 'New initial screen'
            return await super().execute(tool, arguments, **kwargs)
    engine = Changed(artifacts)
    actor = Actor()
    result = asyncio.run(JourneyRunner(engine, artifacts, actor, replay_cache=cache,
        progress=lambda _: None).run(case('http://localhost')))
    assert result.status == 'passed' and actor.sessions == 1
    assert engine.calls.count('browser_click') == 1
    assert read_steps(artifacts)[0]['cache'] == 'stale'


def test_cache_write_failure_does_not_change_a_verified_outcome(tmp_path, monkeypatch):
    cache = ReplayCache(tmp_path / 'cache', tmp_path)
    def denied(recording):
        raise PermissionError('read-only')
    monkeypatch.setattr(cache, 'write', denied)
    result, _, artifacts = execute(tmp_path, cache)
    assert result.status == 'passed'
    assert any(e['result'].get('status') == 'write_error' for e in artifacts.observations)


def test_atomic_replacement_failure_leaves_old_entry_intact(tmp_path, monkeypatch):
    cache = ReplayCache(tmp_path / 'cache', tmp_path)
    execute(tmp_path, cache)
    path = entries(cache)[0]
    previous = path.read_bytes()
    recording = cache.read(path.stem)
    def fail(*args):
        raise OSError('disk failure')
    monkeypatch.setattr('open_verify.replay_cache.os.replace', fail)
    with pytest.raises(OSError):
        cache.write(recording)
    assert path.read_bytes() == previous
    assert list(cache.directory.iterdir()) == [path]


def test_symlink_entry_is_rejected_without_modifying_its_target(tmp_path):
    cache = ReplayCache(tmp_path / 'cache', tmp_path)
    execute(tmp_path, cache)
    path = entries(cache)[0]
    target = tmp_path / 'outside.json'
    target.write_bytes(path.read_bytes())
    before = target.read_bytes()
    path.unlink()
    path.symlink_to(target)
    result, _, artifacts = execute(tmp_path, cache)
    assert result.status == 'passed' and read_steps(artifacts)[0]['cache'] == 'stale'
    assert not path.is_symlink() and target.read_bytes() == before


def test_cli_acp_cold_then_strict_replay_keeps_judge_and_manifest(tmp_path, web_app):
    import sys

    from test_workflow import action, plan

    from open_verify.cli import main

    (tmp_path / '.git').mkdir()
    target = case(web_app, semantic=True)
    planned = plan()
    planned['plan']['cases'] = [target.model_dump()]
    acting = list(Actor().decisions)
    judge = {'explanation': 'Fresh cart screen', 'verdict': 'holds'}
    for mode in ('auto', 'strict'):
        decisions = [planned, action('run_journey', case_id=target.id)]
        if mode == 'auto':
            decisions += [d.model_dump() for d in acting]
        decisions.extend([judge, judge])
        script = tmp_path / f'{mode}.json'
        script.write_text(json.dumps(decisions))
        command = [sys.executable, str(Path(__file__).with_name('fake_agent.py')), str(script)]
        assert main(['Check cart', '--project', str(tmp_path), '--headless',
            '--agent-command', json.dumps(command), '--agent', 'fixture', '--cache', mode,
            '--cache-dir', str(tmp_path / 'cache'), '--output', str(tmp_path / mode),
            '--max-steps', '3', '--agent-timeout', '10']) == 0
        run = next((tmp_path / mode).iterdir())
        manifest = json.loads((run / 'manifest.json').read_text())
        receipt = json.loads(next((run / 'journeys').glob('*.json')).read_text())
        assert manifest['status'] == 'passed' and manifest['schema_version'] == 1
        assert receipt['steps'][0]['model_calls'] == (3 if mode == 'auto' else 1)
        assert receipt['steps'][1]['model_calls'] == 1
        assert not any('cache' in a['path'] for a in manifest['artifacts'])


def test_failed_refresh_invalidates_previous_recording(tmp_path):
    cache = ReplayCache(tmp_path / 'cache', tmp_path)
    execute(tmp_path, cache)
    assert entries(cache)
    result, _, _ = execute(tmp_path, cache, mode='refresh', assertion='failed')
    assert result.status == 'failed' and not entries(cache)


def test_impossible_recorded_action_budget_is_rejected_before_dispatch(tmp_path):
    cache = ReplayCache(tmp_path / 'cache', tmp_path)
    target = case('http://localhost', act_options={'max_actions': 1})
    execute(tmp_path, cache, target=target)
    path = entries(cache)[0]
    data = json.loads(path.read_text())
    data['steps'][0]['actions'] *= 2
    path.write_text(json.dumps(data))
    result, engine, artifacts = execute(tmp_path, cache, mode='strict', target=target, actor=NoActor())
    assert result.status == 'blocked'
    assert 'browser_click' not in engine.calls
    assert read_steps(artifacts)[0]['code'] == 'REPLAY_STALE'


def test_real_replay_export_retains_executed_click_and_exact_check(tmp_path, web_app):
    import os
    import subprocess
    import sys

    cache = ReplayCache(tmp_path / 'cache', tmp_path)
    for actor in (Actor(), NoActor()):
        artifacts = Artifacts(tmp_path / 'runs')
        result = asyncio.run(JourneyRunner(LocalTools(tmp_path, artifacts, headless=True), artifacts,
            actor, replay_cache=cache, progress=lambda _: None).run(case(web_app)))
        assert result.status == 'passed'
    source = artifacts.path / result.test_file
    specification = json.loads(source.with_suffix('.json').read_text())
    assert [s['kind'] for s in specification['steps']] == ['click', 'requires_verification', 'expect_text']
    completed = subprocess.run([sys.executable, str(source)], cwd=artifacts.path,
        env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')},
        capture_output=True, text=True, timeout=30)
    assert completed.returncode == 2 and 'independent goal observer' in completed.stdout + completed.stderr
