"""Real-browser step execution and fail-closed boundaries without a paid model."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_tools import web_app as web_app
from test_workflow import ScriptedAgent, action, plan

from open_verify.agent import ACPDecisionAgent
from open_verify.artifacts import Artifacts
from open_verify.journey import JourneyRunner, StepBudget, StepStopped
from open_verify.journey_spec import ActDecision, ActStep, Judgment
from open_verify.models import Case
from open_verify.runner import VerificationRunner
from open_verify.step_executor import AgentJourneyExecutor
from open_verify.tools import LocalTools


def case(url, *, text="Cart: 1", semantic=False, act_options=None):
    return Case.model_validate({"id": "cart", "title": "Add an item", "interface": "browser",
        "steps": ["Add item", "Check cart"], "expected": text,
        "journey": {"url": url + "/cart", "steps": [
            {"kind": "act", "instruction": "Add an item", **(act_options or {})},
            {"kind": "assert", "instruction": "Cart contains one item",
             **({} if semantic else {"check": {"kind": "expect_text", "text": text}})},
        ]}})


def complete():
    return ActDecision(kind="complete", outcome="done", summary="The item was added")


@pytest.mark.parametrize('changed', [False, True])
def test_recorded_url_comparison_and_exported_replay(tmp_path, web_app, changed):
    artifacts = Artifacts(tmp_path / 'runs')
    actor = Actor([ActDecision.model_validate({'kind': 'action', 'action': {
        'tool': 'browser_open' if changed else 'browser_reload',
        'arguments': {'url': web_app + '/cart'} if changed else {}, 'reason': 'Navigate or reload'}}), complete()])
    target = Case.model_validate({'id': 'persist', 'title': 'Preserve identity', 'interface': 'browser',
        'steps': ['Remember', 'Reload', 'Compare'], 'expected': 'Same URL', 'journey': {'url': web_app,
        'steps': [
            {'kind': 'assert', 'instruction': 'Initial form visible', 'remember_as': 'created',
             'check': {'kind': 'expect_text', 'text': 'Greet'}},
            {'kind': 'act', 'instruction': 'Navigate' if changed else 'Reload'},
            {'kind': 'assert', 'instruction': 'Same detail URL',
             'check': {'kind': 'expect_same_url', 'baseline': 'created'}}]}})
    engine = LocalTools(tmp_path, artifacts, headless=True)
    result = asyncio.run(JourneyRunner(engine, artifacts, actor, progress=lambda _: None).run(target))
    assert result.status == ('failed' if changed else 'passed')
    assert result.checkpoints[0].status == 'passed'
    source = (artifacts.path / result.test_file).read_text()
    assert "saved_urls['created'] = page.url" in source
    if not changed:
        replay = subprocess.run([sys.executable, result.test_file], cwd=artifacts.path,
            env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')},
            capture_output=True, text=True, timeout=30)
        assert replay.returncode != 0 and 'independent goal observer' in replay.stdout + replay.stderr, replay.stdout + replay.stderr


def test_semantic_comparison_gets_host_baseline_without_actor_claims(tmp_path, web_app):
    artifacts = Artifacts(tmp_path / 'runs')
    actor = Actor([ActDecision.model_validate({'kind': 'action', 'action': {
        'tool': 'browser_reload', 'arguments': {}, 'reason': 'Reload'}}), complete()])
    target = Case.model_validate({'id': 'compare', 'title': 'Compare observations', 'interface': 'browser',
        'steps': ['Remember', 'Reload', 'Compare'], 'expected': 'Same content', 'journey': {'url': web_app,
        'steps': [
            {'kind': 'assert', 'instruction': 'Form visible', 'remember_as': 'before',
             'check': {'kind': 'expect_text', 'text': 'Greet'}},
            {'kind': 'act', 'instruction': 'Reload'},
            {'kind': 'assert', 'instruction': 'Same form after reload', 'compare_to': 'before'}]}})
    result = asyncio.run(JourneyRunner(LocalTools(tmp_path, artifacts, headless=True), artifacts,
        actor, progress=lambda _: None).run(target))
    assert result.status == 'passed'
    evidence = actor.judgments[0][1]
    assert evidence['url'] == evidence['baseline']['url']
    assert 'Greet' in evidence['baseline']['snapshot']
    assert evidence['baseline']['evidence_id'].startswith('E')
    assert 'summary' not in evidence['baseline']


def test_journey_rejects_unknown_and_duplicate_baselines():
    from open_verify.journey_spec import BrowserJourney
    with pytest.raises(ValueError, match='earlier remembered'):
        BrowserJourney(url='http://localhost', steps=[{'kind':'assert', 'instruction':'Same',
            'check': {'kind':'expect_same_url', 'baseline':'missing'}}])
    with pytest.raises(ValueError, match='unique'):
        BrowserJourney(url='http://localhost', steps=[{'kind':'assert', 'instruction': label,
            'remember_as':'duplicate'} for label in ('One', 'Two')])


def test_relative_entry_binds_discovered_origin_and_exports_absolute_replay(tmp_path, web_app):
    artifacts = Artifacts(tmp_path / 'runs')
    engine = LocalTools(tmp_path, artifacts, headless=True)
    runner = VerificationRunner(None, engine, artifacts, journey_executor=Actor(), progress=lambda _: None)
    target = case(web_app)
    target.journey.url = '/cart'
    state = {'stage': 'execute', 'plan': {'cases': [target.model_dump()]}, 'findings': []}
    async def run():
        try:
            missing = await runner.run_journey(state, {'case_id': target.id})
            assert not missing['ok'] and 'requires base_url' in missing['result']['error']
            assert not runner.test_results  # Address repair must not consume the case.
            receipt = await runner.run_journey(state, {'case_id': target.id, 'base_url': web_app})
            assert receipt['result']['status'] == 'passed', receipt
            assert state['plan']['cases'][0]['journey']['url'] == '/cart'
            return runner.test_results[0]
        finally:
            await engine.close()
    result = asyncio.run(run())
    source = (artifacts.path / result.test_file).read_text()
    assert web_app + '/cart' in source
    replay = subprocess.run([sys.executable, result.test_file], cwd=artifacts.path,
        env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')},
        capture_output=True, text=True, timeout=30)
    assert replay.returncode == 2 and 'independent goal observer' in replay.stdout + replay.stderr, replay.stdout + replay.stderr


@pytest.mark.parametrize('origin', ['https://user:pass@example.com', '//example.com',
                                  'file:///tmp', 'http://localhost/path', 'http://localhost?x=1'])
def test_runtime_base_rejects_credentials_and_non_origin_values(origin):
    from open_verify.journey_spec import RunJourney
    with pytest.raises(ValueError):
        RunJourney(case_id='cart', base_url=origin)


def test_runtime_base_cannot_override_absolute_plan_or_external_origin(tmp_path):
    artifacts = Artifacts(tmp_path / 'runs')
    runner = VerificationRunner(None, LocalTools(tmp_path, artifacts), artifacts, progress=lambda _: None)
    target = case('http://localhost:8000')
    state = {'stage': 'execute', 'plan': {'cases': [target.model_dump()]}, 'findings': []}
    async def run():
        receipt = await runner.run_journey(state, {'case_id': target.id, 'base_url': 'http://localhost:9000'})
        assert not receipt['ok'] and 'cannot be overridden' in receipt['result']['error']
        state['plan']['cases'][0]['journey']['url'] = '/cart'
        receipt = await runner.run_journey(state, {'case_id': target.id, 'base_url': 'https://example.com'})
        assert not receipt['ok'] and not runner.test_results
    asyncio.run(run())


class Actor:
    def __init__(self, decisions=None, verdict="holds"):
        self.decisions = iter(decisions or [ActDecision.model_validate({"kind": "action", "action": {
            "tool": "browser_click", "arguments": {"by": "role", "role": "button", "name": "Add item"},
            "reason": "Add item"}}), complete()])
        self.verdict = verdict
        self.contexts = []
        self.judgments = []
        self.sessions = 0

    async def begin(self):
        self.sessions += 1

    async def act(self, context, *, on_call):
        on_call()
        self.contexts.append(context)
        return next(self.decisions)

    async def judge_goal(self, instruction, observation, *, on_call):
        on_call()
        return Judgment(explanation='Measured action goal reached', verdict='holds')

    async def judge(self, instruction, observation, *, on_call):
        on_call()
        self.judgments.append((instruction, observation))
        return Judgment(explanation="Observed current cart", verdict=self.verdict)


@pytest.mark.parametrize(("text", "expected"), [("Cart: 1", "passed"), ("Cart: 2", "failed")])
def test_real_journey_and_exported_replay(tmp_path, web_app, text, expected):
    artifacts = Artifacts(tmp_path / "runs")
    engine = LocalTools(tmp_path, artifacts, headless=True)
    actor = Actor()
    result = asyncio.run(JourneyRunner(engine, artifacts, actor, progress=lambda _: None).run(case(web_app, text=text)))
    assert result.status == expected
    assert result.screenshots
    assert actor.sessions == 1
    receipt = json.loads(next((artifacts.path / 'journeys').glob('*.json')).read_text())
    assert [s['kind'] for s in receipt['steps']] == ['act', 'assert']
    assert [s['model_calls'] for s in receipt['steps']] == [3, 0]
    assert [s['actions'] for s in receipt['steps']] == [1, 0]
    replay = subprocess.run([sys.executable, result.test_file], cwd=artifacts.path,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / 'src')},
        capture_output=True, text=True, timeout=30)
    assert replay.returncode != 0 and 'independent goal observer' in replay.stdout + replay.stderr, replay.stdout + replay.stderr


@pytest.mark.parametrize(("verdict", "status"), [('holds', 'passed'), ('fails', 'failed'), ('inconclusive', 'blocked')])
def test_semantic_judge_sees_fresh_screen_and_replay_cannot_claim_success(tmp_path, web_app, verdict, status):
    artifacts = Artifacts(tmp_path / 'runs')
    actor = Actor(verdict=verdict)
    result = asyncio.run(JourneyRunner(LocalTools(tmp_path, artifacts, headless=True), artifacts,
        actor, progress=lambda _: None).run(case(web_app, semantic=True)))
    assert result.status == status
    requirement, screen = actor.judgments[0]
    assert requirement == 'Cart contains one item'
    assert 'Cart: 1' in screen['snapshot']
    assert set(screen) <= {'url', 'snapshot', 'truncated'}
    source = (artifacts.path / result.test_file).read_text()
    assert 'raise RuntimeError' in source and 'Live semantic judgment required' in source
    assert result.omissions


def test_actor_claim_cannot_replace_assertion(tmp_path, web_app):
    artifacts = Artifacts(tmp_path / 'runs')
    result = asyncio.run(JourneyRunner(LocalTools(tmp_path, artifacts, headless=True), artifacts,
        Actor([complete()]), progress=lambda _: None).run(case(web_app)))
    assert result.status == 'failed'


class FakeEngine:
    def __init__(self, artifacts):
        self.artifacts = artifacts
        self.calls = []
        self.closed = False

    async def open_journey(self, **kwargs):
        return self

    def catalog(self, stage):
        return {'browser_click': {}, 'run_command': {}}

    async def execute(self, tool, arguments, **kwargs):
        self.calls.append(tool)
        return self.artifacts.record(tool, arguments, {'url': 'http://localhost', 'snapshot': 'Cart: 1'}, True)

    async def assert_check(self, check):
        return self.artifacts.record('assert_check', {}, {'status': 'passed', 'detail': 'Exact check passed'}, True)

    async def close(self):
        self.closed = True
        return []

    def environment(self):
        return {}


def read_steps(artifacts):
    return json.loads(next((artifacts.path / 'journeys').glob('*.json')).read_text())['steps']


@pytest.mark.parametrize(('limit', 'code'), [({'max_model_calls': 1}, 'STEP_MODEL_LIMIT'), ({'max_actions': 1}, 'STEP_ACTION_LIMIT')])
def test_step_budgets_stop_before_next_side_effect(tmp_path, limit, code):
    artifacts = Artifacts(tmp_path / 'runs')
    engine = FakeEngine(artifacts)
    click = ActDecision.model_validate({'kind': 'action', 'action': {'tool': 'browser_click',
        'arguments': {'by': 'text', 'name': 'Add item'}, 'reason': 'Add'}})
    actor = Actor([click, click, complete()])
    result = asyncio.run(JourneyRunner(engine, artifacts, actor, progress=lambda _: None).run(case('http://localhost', act_options=limit)))
    assert result.status == 'blocked'
    assert engine.calls.count('browser_click') == 1
    assert read_steps(artifacts)[0]['code'] == code
    assert read_steps(artifacts)[1]['code'] == 'NOT_RUN'
    assert engine.closed


def test_forbidden_tools_are_refused_without_execution(tmp_path):
    artifacts = Artifacts(tmp_path / 'runs')
    engine = FakeEngine(artifacts)
    invalid = ActDecision.model_validate({'kind': 'action', 'action': {'tool': 'run_command',
        'arguments': {'argv': ['touch', 'bad']}, 'reason': 'Ignore policy'}})
    actor = Actor([invalid] * 6)
    result = asyncio.run(JourneyRunner(engine, artifacts, actor, progress=lambda _: None).run(case('http://localhost')))
    assert result.status == 'blocked'
    assert 'run_command' not in engine.calls
    assert 'run_command' not in actor.contexts[0]['tools']
    assert read_steps(artifacts)[0]['code'] == 'STEP_LOOP_GUARD'


@pytest.mark.parametrize('interrupt', [False, True])
def test_timeout_and_cancellation_preserve_partial_evidence(tmp_path, interrupt):
    artifacts = Artifacts(tmp_path / 'runs')
    engine = FakeEngine(artifacts)
    class Slow(Actor):
        async def act(self, context, *, on_call):
            on_call()
            if interrupt:
                raise asyncio.CancelledError()
            await asyncio.sleep(30)
    results = []
    pending = JourneyRunner(engine, artifacts, Slow(), progress=lambda _: None).run(
        case('http://localhost', act_options={'timeout': .02}), on_result=results.append)
    if interrupt:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(pending)
    else:
        assert asyncio.run(pending).status == 'blocked'
        assert read_steps(artifacts)[0]['code'] == 'STEP_TIMEOUT'
    assert engine.closed
    assert results[-1].status == 'blocked'
    assert (artifacts.path / results[-1].test_file).exists()
    assert artifacts.observations


def test_judge_starts_new_conversation_and_has_no_actor_transcript():
    class Transport:
        def __init__(self):
            self.resets = 0
            self.prompts = []
        async def reset_session(self):
            self.resets += 1
        async def respond(self, prompt, schema, *, on_call):
            on_call()
            self.prompts.append(prompt)
            return complete() if schema is ActDecision else Judgment(explanation='Screen', verdict='holds')
    transport = Transport()
    executor = AgentJourneyExecutor(transport)
    async def run():
        await executor.begin()
        await executor.act({'goal': 'actor-private-value'}, on_call=lambda: None)
        await executor.judge('Requirement', {'snapshot': 'fresh'}, on_call=lambda: None)
        await executor.begin()
    asyncio.run(run())
    assert transport.resets == 3
    assert 'actor-private-value' not in transport.prompts[-1]
    assert 'fresh' in transport.prompts[-1]


def test_acp_repair_consumes_model_budget(tmp_path):
    from langgraph_acp import ACPEventType
    class Session:
        calls = 0
        async def prompt(self, prompt):
            self.calls += 1
            yield SimpleNamespace(type=ACPEventType.MESSAGE_DELTA, data={'content': {'type': 'text', 'text': '{}'}})
            yield SimpleNamespace(type=ACPEventType.PROMPT_COMPLETED, data={'stopReason': 'end_turn'})
    agent = ACPDecisionAgent(None, tmp_path)
    agent.client = object()
    agent.session = Session()
    budget = StepBudget(ActStep(kind='act', instruction='Add', max_model_calls=1))
    with pytest.raises(StepStopped, match='model-call budget'):
        asyncio.run(agent.respond('Return JSON', ActDecision, on_call=budget.model_call))
    assert agent.session.calls == 1
    assert budget.model_calls == 1


def test_workflow_finishes_from_host_journey_result(tmp_path, web_app):
    planned = plan()
    planned['plan']['cases'] = [case(web_app).model_dump()]
    agent = ScriptedAgent([planned, action('run_journey', case_id='cart')])
    artifacts = Artifacts(tmp_path / 'runs')
    engine = LocalTools(tmp_path, artifacts, headless=True)
    runner = VerificationRunner(agent, engine, artifacts, journey_executor=Actor(), progress=lambda _: None)
    report = asyncio.run(runner.run('Check cart'))
    assert report['status'] == 'complete'
    assert report['findings'][0]['status'] == 'passed'
    assert report['plan']['cases'][0]['checks'] == ['Cart contains one item']
    assert len(runner.test_results) == 1
    assert json.loads((artifacts.path / 'manifest.json').read_text())['status'] == 'passed'


def test_missing_authentication_blocks_before_application_navigation(tmp_path, web_app):
    artifacts = Artifacts(tmp_path / 'runs')
    target = case(web_app)
    target.journey.authenticated = True
    result = asyncio.run(JourneyRunner(LocalTools(tmp_path, artifacts, headless=True), artifacts,
        Actor(), progress=lambda _: None).run(target))
    assert result.status == 'blocked'
    assert 'Authentication is missing' in result.detail
    assert not any(e['tool'] == 'browser_open' for e in artifacts.observations)


def test_cases_have_separate_browser_contexts(tmp_path, web_app):
    artifacts = Artifacts(tmp_path / 'runs')
    engine = LocalTools(tmp_path, artifacts, headless=True)
    async def run():
        first = await JourneyRunner(engine, artifacts, Actor(), progress=lambda _: None).run(case(web_app))
        target = case(web_app, text='Cart: 0')
        target.id = 'initial-cart'
        target.journey.steps.pop(0)
        second = await JourneyRunner(engine, artifacts, None, progress=lambda _: None).run(target)
        return first, second
    assert all(r.status == 'passed' for r in asyncio.run(run()))
    assert engine.context is None


@pytest.mark.parametrize('change', ['missing-check', 'duplicate-check', 'terminal', 'ends-with-act'])
def test_invalid_journeys_are_rejected_before_execution(change):
    raw = case('http://localhost').model_dump()
    if change == 'missing-check':
        raw['checks'] = ['A different required check']
    elif change == 'duplicate-check':
        raw['journey']['steps'].append(raw['journey']['steps'][-1])
    elif change == 'terminal':
        raw['interface'] = 'terminal'
    else:
        raw['journey']['steps'].pop()
    with pytest.raises(ValueError):
        Case.model_validate(raw)


def test_model_cannot_finalize_structured_case_from_unrelated_evidence(tmp_path):
    from open_verify.models import Finding
    artifacts = Artifacts(tmp_path / 'runs')
    runner = VerificationRunner(None, LocalTools(tmp_path, artifacts), artifacts)
    evidence = artifacts.record('browser_open', {}, {'snapshot': 'Looks good'}, True)
    state = {'plan': {'cases': [case('http://localhost').model_dump()]}, 'findings': []}
    error = runner.validate_finding(state, Finding(case_id='cart', status='passed', actual='All good', evidence=[evidence['id']]))
    assert error and 'latest generated test' in error


def test_oversized_trace_exports_barrier_without_losing_live_result(tmp_path):
    artifacts = Artifacts(tmp_path / 'runs')
    engine = FakeEngine(artifacts)
    target = case('http://localhost', act_options={'max_actions': 45, 'max_model_calls': 50})
    decisions = [ActDecision.model_validate({'kind': 'action', 'action': {'tool': 'browser_click',
        'arguments': {'by': 'text', 'name': f'Item {i}'}, 'reason': 'Select'}}) for i in range(41)]
    result = asyncio.run(JourneyRunner(engine, artifacts, Actor([*decisions, complete()]),
        progress=lambda _: None).run(target))
    assert result.status == 'passed'
    assert read_steps(artifacts)[0]['actions'] == 41
    assert any('40 operations' in reason for reason in result.omissions)
    assert 'raise RuntimeError' in (artifacts.path / result.test_file).read_text()


def test_cancelled_journey_remains_in_workflow_manifest(tmp_path):
    from test_architecture import FixtureExecutor
    artifacts = Artifacts(tmp_path / 'runs')
    engine = LocalTools(tmp_path, artifacts)
    scripted = plan()
    scripted['plan']['cases'] = [case('http://localhost').model_dump()]
    runner = VerificationRunner(None, engine, artifacts,
        executor=FixtureExecutor([scripted, action('run_journey', case_id='cart')]), progress=lambda _: None)
    runner.journeys.engine = FakeEngine(artifacts)
    class CancelActor(Actor):
        async def act(self, context, *, on_call):
            on_call()
            raise asyncio.CancelledError()
    runner.journeys.executor = CancelActor()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runner.run('Check cart'))
    manifest = json.loads((artifacts.path / 'manifest.json').read_text())
    assert manifest['status'] == 'incomplete'
    assert manifest['tests'][0]['status'] == 'blocked'
    assert read_steps(artifacts)[0]['code'] == 'STEP_INTERRUPTED'
    assert read_steps(artifacts)[0]['model_calls'] == 1


@pytest.mark.parametrize(('check', 'expected'), [
    ({'kind': 'expect_url', 'url': 'ENTRY/cart'}, 'passed'),
    ({'kind': 'expect_text', 'text': 'Cart: 99', 'visible': False}, 'passed'),
    ({'kind': 'expect_json', 'path': '/api', 'field': ['items', 1], 'value': 2}, 'passed'),
    ({'kind': 'expect_json', 'path': '/api', 'field': ['missing'], 'value': 2}, 'failed'),
])
def test_exact_assertions_need_no_model(tmp_path, web_app, check, expected):
    artifacts = Artifacts(tmp_path / 'runs')
    target = case(web_app).model_dump()
    target['journey']['steps'] = [{'kind': 'assert', 'instruction': 'Exact check',
        'check': json.loads(json.dumps(check).replace('ENTRY', web_app))}]
    result = asyncio.run(JourneyRunner(LocalTools(tmp_path, artifacts, headless=True), artifacts,
        None, progress=lambda _: None).run(Case.model_validate(target)))
    assert result.status == expected
    assert read_steps(artifacts)[0]['model_calls'] == 0
    if check['kind'] == 'expect_json':
        replay = subprocess.run([sys.executable, result.test_file], cwd=artifacts.path,
            env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')},
            capture_output=True, text=True, timeout=30)
        assert replay.returncode == (0 if expected == 'passed' else 1), replay.stdout + replay.stderr


def test_journey_media_and_interruption_checkpoint(tmp_path, web_app, monkeypatch):
    artifacts = Artifacts(tmp_path / 'runs')
    engine = LocalTools(tmp_path, artifacts, headless=True)
    snapshots = []
    async def interrupted_encoder(screenshots, destination):
        assert len(screenshots) >= 2
        assert all(p.is_file() for p in screenshots)
        raise asyncio.CancelledError()
    monkeypatch.setattr('open_verify.journey.encode_gif', interrupted_encoder)
    def checkpoint(result):
        snapshots.append(result.model_copy(deep=True))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(JourneyRunner(engine, artifacts, Actor(), progress=lambda _: None).run(
            case(web_app), capture_media=True, on_result=checkpoint))
    assert snapshots[0].status == 'passed'
    assert snapshots[-1].status == 'passed'
    assert 'GIF omitted: encoding was interrupted.' in snapshots[-1].omissions
    assert not any('has not completed' in s for s in snapshots[-1].omissions)


@pytest.mark.parametrize("status", ["passed", "failed", "blocked"])
def test_actor_cannot_submit_a_verdict(status):
    with pytest.raises(ValueError):
        ActDecision.model_validate({
            "kind": "complete", "status": status, "summary": "Actor claims a verdict"
        })


def test_actor_done_cannot_overrule_independent_judge(tmp_path):
    artifacts = Artifacts(tmp_path / "runs")
    actor = Actor([complete()], verdict="fails")
    result = asyncio.run(JourneyRunner(FakeEngine(artifacts), artifacts, actor,
        progress=lambda _: None).run(case("http://localhost", semantic=True)))
    assert result.status == "failed"
    steps = read_steps(artifacts)
    assert [step["status"] for step in steps] == ["passed", "failed"]
    assert len(actor.judgments) == 1


def test_actor_blocked_never_becomes_a_product_failure(tmp_path):
    artifacts = Artifacts(tmp_path / "runs")
    actor = Actor([ActDecision(kind="complete", outcome="blocked", summary="Cannot click")])
    result = asyncio.run(JourneyRunner(FakeEngine(artifacts), artifacts, actor,
        progress=lambda _: None).run(case("http://localhost", semantic=True)))
    assert result.status == "blocked"
    assert not actor.judgments
    assert read_steps(artifacts)[1]["code"] == "NOT_RUN"


@pytest.mark.parametrize("ready", [False, True])
def test_readiness_checks_observable_app_state_without_actor_actions(tmp_path, ready):
    artifacts = Artifacts(tmp_path / "runs")

    class ReadinessEngine(FakeEngine):
        async def assert_check(self, check):
            return self.artifacts.record("assert_check", {}, {
                "status": "passed" if ready else "failed",
                "detail": "Fixture visible" if ready else "Fixture missing from app",
            }, ready)

    from open_verify.journey_spec import AssertStep

    target = case("http://localhost")
    target.journey.readiness = [AssertStep(kind="assert", instruction="Fixture visible",
        check={"kind": "expect_text", "text": "Disposable WorkOrder"})]
    engine = ReadinessEngine(artifacts)
    actor = Actor()
    readiness = asyncio.run(JourneyRunner(engine, artifacts, actor,
        progress=lambda _: None).check_readiness(target))
    assert readiness["status"] == ("passed" if ready else "blocked")
    assert not actor.contexts
    assert "browser_click" not in engine.calls
    assert engine.closed
    assert not list((artifacts.path / "tests").glob("*.py"))


@pytest.mark.parametrize("recovers", [False, True])
def test_runner_gates_journey_and_bounds_setup_recovery(tmp_path, recovers):
    from open_verify.test_spec import TestResult

    artifacts = Artifacts(tmp_path / "runs")
    runner = VerificationRunner(None, LocalTools(tmp_path, artifacts), artifacts,
        progress=lambda _: None)
    target = case("http://localhost")
    from open_verify.journey_spec import AssertStep

    target.journey.readiness = [AssertStep(kind="assert", instruction="Fixture visible",
        check={"kind": "expect_text", "text": "Disposable WorkOrder"})]
    # Serialize first so the runner validates the immutable plan as production does.
    state = {"stage": "execute", "plan": {"cases": [target.model_dump()]}, "findings": []}

    class Journeys:
        checks = 0
        runs = 0

        async def check_readiness(self, current):
            self.checks += 1
            assert current.journey.readiness[0].instruction == "Fixture visible"
            ready = recovers and self.checks == 2
            return {"case_id": current.id, "status": "passed" if ready else "blocked",
                "detail": "Fixture visible" if ready else "Fixture missing from app"}

        async def run(self, current, **kwargs):
            self.runs += 1
            return TestResult(case_id=current.id, status="passed", detail="Independent check passed",
                test_file="fixture.py", rerun=[])

    journeys = Journeys()
    runner.journeys = journeys

    async def exercise():
        first = await runner.run_journey(state, {"case_id": target.id})
        assert not first["ok"]
        assert first["result"]["code"] == "SETUP_NOT_READY"
        assert first["result"]["remaining_repairs"] == 2
        assert not journeys.runs and not runner.test_results
        second = await runner.run_journey(state, {"case_id": target.id})
        if recovers:
            assert second["ok"] and second["result"]["status"] == "passed"
            assert journeys.runs == 1 and len(runner.test_results) == 1
        else:
            assert not second["ok"] and second["result"]["remaining_repairs"] == 1
            third = await runner.run_journey(state, {"case_id": target.id})
            assert third["ok"] and third["result"]["status"] == "blocked"
            assert "Journey not run" in third["result"]["detail"]
            assert not journeys.runs and not runner.test_results
        assert state["plan"]["cases"][0]["journey"]["steps"] == target.model_dump()["journey"]["steps"]

    asyncio.run(exercise())


@pytest.mark.parametrize("visible", [True, False])
def test_real_browser_readiness_gates_fixed_journey(tmp_path, web_app, visible):
    from open_verify.journey_spec import AssertStep

    artifacts = Artifacts(tmp_path / "runs")
    target = case(web_app)
    target.journey.readiness = [AssertStep(kind="assert", instruction="Cart starts empty",
        check={"kind": "expect_text", "text": "Cart: 0" if visible else "Missing fixture"},
        timeout=15 if visible else 2)]
    planned = plan()
    planned["plan"]["cases"] = [target.model_dump()]
    calls = [action("run_journey", case_id=target.id)] * (1 if visible else 3)
    actor = Actor()
    runner = VerificationRunner(ScriptedAgent([planned, *calls]),
        LocalTools(tmp_path, artifacts, headless=True), artifacts,
        journey_executor=actor, progress=lambda _: None)
    report = asyncio.run(runner.run("Check cart"))
    assert report["findings"][0]["status"] == ("passed" if visible else "blocked")
    readiness = [e for e in artifacts.observations if e["tool"] == "setup_readiness"]
    assert len(readiness) == (1 if visible else 3)
    if visible:
        assert actor.contexts and len(runner.test_results) == 1
    else:
        assert not actor.contexts and len(runner.test_results) == 1
        assert runner.test_results[0].status == 'blocked'
        assert runner.test_results[0].screenshots
        assert all(p.code == 'NOT_RUN' for p in runner.test_results[0].checkpoints[1:])
