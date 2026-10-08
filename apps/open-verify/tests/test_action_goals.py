"""Action completion must be independently evidenced across unrelated product surfaces."""

import asyncio

import pytest
from test_journeys import Actor, complete, read_steps
from test_tools import web_app as web_app

from open_verify.artifacts import Artifacts
from open_verify.health import ApplicationHealth
from open_verify.journey import JourneyRunner
from open_verify.journey_spec import ActDecision, HealthJudgment, Judgment
from open_verify.models import Case
from open_verify.step_executor import AgentJourneyExecutor
from open_verify.tools import LocalTools


def navigate(url):
    return ActDecision.model_validate({'kind': 'action', 'action': {
        'tool': 'browser_open', 'arguments': {'url': url}, 'reason': 'Navigate'}})


@pytest.mark.parametrize('destination,visible', [('/cart', 'Add item'), ('/', 'Greet')])
@pytest.mark.parametrize('recover', [True, False])
def test_wrong_destination_cannot_complete_and_has_one_bounded_recovery(tmp_path, web_app, destination, visible, recover):
    wrong = '/' if destination == '/cart' else '/cart'
    class GoalActor(Actor):
        goals = []
        async def judge_goal(self, instruction, observation, *, on_call):
            on_call()
            self.goals.append(observation)
            assert 'summary' not in observation and 'completed_steps' not in observation
            assert observation['host_operations']
            holds = observation['url'] == web_app + destination
            return Judgment(verdict='holds' if holds else 'fails',
                explanation='Requested destination reached' if holds else 'The destination does not match the requested goal')
    actor = GoalActor([navigate(web_app + wrong), complete(),
        *([navigate(web_app + destination)] if recover else []), complete()])
    artifacts = Artifacts(tmp_path / 'runs')
    target = Case.model_validate({'id': 'navigation', 'title': 'Open requested destination',
        'interface': 'browser', 'steps': ['Open', 'Check controls'], 'expected': visible,
        'journey': {'url': web_app, 'steps': [
            {'kind': 'act', 'instruction': 'Open ' + destination},
            {'kind': 'assert', 'instruction': 'Expected controls appear',
             'check': {'kind': 'expect_text', 'text': visible}}]}})
    result = asyncio.run(JourneyRunner(LocalTools(tmp_path, artifacts, headless=True), artifacts,
        actor, progress=lambda _: None, cache_mode='off').run(target))
    assert result.status == ('passed' if recover else 'blocked')
    goals = [e for e in artifacts.observations if e['tool'] == 'action_goal']
    assert [e['result']['verdict'] for e in goals] == ['fails', 'holds' if recover else 'fails']
    assert actor.sessions == 2
    assert read_steps(artifacts)[0]['model_calls'] == (6 if recover else 5)
    assert 'Independent goal check rejected completion' in actor.contexts[2]['feedback']
    if not recover:
        assert read_steps(artifacts)[0]['code'] == 'ACTION_GOAL_NOT_REACHED'
        assert result.checkpoints[0].code == 'NOT_RUN'


def test_goal_judge_resets_transport_and_does_not_receive_actor_completion():
    class Transport:
        resets = 0
        async def reset_session(self):
            self.resets += 1
        async def respond(self, prompt, schema, *, on_call):
            on_call()
            assert 'successful operation is insufficient' in prompt
            assert 'actor summary' not in prompt
            return schema(verdict='fails', explanation='Requested form is absent')
    agent = Transport()
    judgment = asyncio.run(AgentJourneyExecutor(agent).judge_goal('Open editor',
        {'snapshot': 'Search results', 'url': 'http://localhost/search'}, on_call=lambda: None))
    assert agent.resets == 1 and judgment.verdict == 'fails'


@pytest.mark.parametrize('diagnosis,code', [('unknown', 'APP_HEALTH_INCONCLUSIVE'),
    ('action', 'QA_ACTION_ERROR'), ('application', 'UNEXPECTED_APP_ERROR')])
def test_health_does_not_label_unattributed_mismatch_an_application_error(tmp_path, diagnosis, code):
    from test_application_health import EvidenceEngine
    artifacts = Artifacts(tmp_path / 'runs')
    engine = EvidenceEngine(artifacts)
    class Observer:
        async def judge_health(self, instruction, evidence, *, on_call):
            on_call()
            assert evidence['failed_actions'][0]['instruction'] == 'Open editor'
            return HealthJudgment(verdict='fails', diagnosis=diagnosis,
                explanation='Expected destination was not reached')
    case = type('Target', (), {'expected': 'Editor', 'checks': []})()
    point = asyncio.run(ApplicationHealth(engine, artifacts, Observer()).inspect(engine, case, {},
        failed_actions=[{'instruction': 'Open editor', 'code': 'ACTION_GOAL_NOT_REACHED'}]))
    assert point['status'] == 'blocked' and point['code'] == code
    if diagnosis != 'application':
        assert 'Unexpected application error' not in point['detail']


def test_inconclusive_goal_does_not_retry_or_advance(tmp_path):
    from test_journeys import FakeEngine, case
    artifacts = Artifacts(tmp_path / 'runs')
    class Uncertain(Actor):
        async def judge_goal(self, instruction, observation, *, on_call):
            on_call()
            return Judgment(verdict='inconclusive', explanation='Destination identity is not exposed')
    actor = Uncertain([complete()])
    result = asyncio.run(JourneyRunner(FakeEngine(artifacts), artifacts, actor,
        progress=lambda _: None).run(case('http://localhost')))
    assert result.status == 'blocked'
    assert read_steps(artifacts)[0]['code'] == 'ACTION_GOAL_UNCONFIRMED'
    assert read_steps(artifacts)[1]['code'] == 'NOT_RUN'
    assert actor.sessions == 1


def test_two_call_reload_has_reserved_independent_goal_capacity(tmp_path, web_app):
    artifacts = Artifacts(tmp_path / 'runs')
    class ReloadObserver(Actor):
        async def judge_goal(self, instruction, observation, *, on_call):
            on_call()
            assert any(e['tool'] == 'browser_reload' and e['ok'] for e in observation['host_operations'])
            assert 'Greet' in observation['snapshot']
            return Judgment(verdict='holds', explanation='Host reloaded the page and the form remains visible')
    actor = ReloadObserver([ActDecision.model_validate({'kind': 'action', 'action': {
        'tool': 'browser_reload', 'arguments': {}, 'reason': 'Reload'}}), complete()])
    target = Case.model_validate({'id': 'reload', 'title': 'Reload page', 'interface': 'browser',
        'steps': ['Reload', 'Check form'], 'expected': 'Form remains visible',
        'journey': {'url': web_app, 'steps': [
            {'kind': 'act', 'instruction': 'Reload this page', 'max_model_calls': 2, 'max_actions': 1},
            {'kind': 'assert', 'instruction': 'Form remains visible',
             'check': {'kind': 'expect_text', 'text': 'Greet'}}]}})
    result = asyncio.run(JourneyRunner(LocalTools(tmp_path, artifacts, headless=True), artifacts,
        actor, progress=lambda _: None, cache_mode='off').run(target))
    assert result.status == 'passed'
    assert read_steps(artifacts)[0]['model_calls'] == 3
    assert read_steps(artifacts)[0]['actions'] == 1
    assert actor.contexts[-1]['remaining_model_calls'] == 1


def test_goal_provider_repair_cannot_exceed_reserved_allowance(tmp_path):
    from test_journeys import FakeEngine, case
    class OverspendingObserver(Actor):
        async def judge_goal(self, instruction, observation, *, on_call):
            on_call()
            on_call()
            on_call()
            return Judgment(verdict='holds', explanation='Not accepted')
    artifacts = Artifacts(tmp_path / 'runs')
    actor = OverspendingObserver([complete()])
    result = asyncio.run(JourneyRunner(FakeEngine(artifacts), artifacts, actor,
        progress=lambda _: None).run(case('http://localhost', act_options={'max_model_calls': 1})))
    assert result.status == 'blocked'
    assert read_steps(artifacts)[0]['code'] == 'ACTION_GOAL_MODEL_LIMIT'
    assert read_steps(artifacts)[0]['model_calls'] == 3
    assert result.checkpoints[0].code == 'NOT_RUN'
