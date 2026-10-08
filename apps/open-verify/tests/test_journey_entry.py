"""Journey entry discovery is checked before any product action, with bounded route repair."""

import asyncio
import copy
import json

import pytest
from test_journeys import Actor, case
from test_tools import web_app as web_app

from open_verify.artifacts import Artifacts
from open_verify.entry import EntryPreparation
from open_verify.journey_spec import JourneyEntry
from open_verify.runner import VerificationRunner
from open_verify.tools import LocalTools


def entry(artifacts):
    receipt = artifacts.record('read_file', {'path': 'routes.py'},
        {'text': 'GET /cart renders the cart and its Add item control.'}, True)
    return JourneyEntry.model_validate({'evidence': [receipt['id']], 'controls': [
        {'kind': 'assert', 'instruction': 'Add item control visible', 'timeout': 1,
         'check': {'kind': 'expect_text', 'text': 'Add item'}}]})


@pytest.mark.parametrize('missing', [True, False])
def test_graph_rejects_missing_or_invented_evidence_before_opening_browser(tmp_path, missing):
    artifacts = Artifacts(tmp_path / 'runs')
    target = case('http://localhost')
    if not missing:
        target.journey.entry = entry(artifacts)
        target.journey.entry.evidence = ['E9999']

    class NeverOpen:
        async def check_readiness(self, target):
            pytest.fail('Invalid entry evidence must stop before live readiness')

    stage = EntryPreparation(NeverOpen(), artifacts)
    assert {'validate_entry_evidence', 'verify_entry_readiness'} <= set(stage.graph.nodes)
    result = asyncio.run(stage.run(target, required=True))
    assert result['status'] == 'blocked'
    assert result['code'] == ('ENTRY_CONTRACT_REQUIRED' if missing else 'ENTRY_EVIDENCE_INVALID')


def test_wrong_route_can_be_repaired_without_changing_case_and_controls(tmp_path, web_app):
    artifacts = Artifacts(tmp_path / 'runs')
    target = case(web_app)
    target.journey.entry = entry(artifacts)
    target.journey.url = web_app
    actor = Actor()
    engine = LocalTools(tmp_path, artifacts, headless=True)
    runner = VerificationRunner(None, engine, artifacts, journey_executor=actor, progress=lambda _: None)
    state = {'stage': 'execute', 'plan': {'cases': [target.model_dump()]}, 'findings': []}
    original = copy.deepcopy(state['plan']['cases'][0])
    request = {'case_id': target.id, 'url': web_app + '/cart',
               'entry': target.journey.entry.model_dump(), 'reason': 'Inspected cart route'}

    async def run():
        try:
            assert not runner.repair_journey_entry(state, request)['ok']
            blocked = await runner.run_journey(state, {'case_id': target.id})
            assert not blocked['ok'] and blocked['result']['remaining_repairs'] == 2
            assert actor.sessions == 0 and not runner.test_results
            for update in ({'url': 'http://elsewhere/cart'},
                           {'entry': {**request['entry'], 'evidence': ['E9999']}},
                           {'entry': {**request['entry'], 'controls': [
                               {'kind': 'assert', 'instruction': 'Weaker check',
                                'check': {'kind': 'expect_text', 'text': 'Greet'}}]}}):
                assert not runner.repair_journey_entry(state, {**request, **update})['ok']
            assert state['plan']['cases'][0] == original
            assert runner.repair_journey_entry(state, request)['ok']
            assert not runner.repair_journey_entry(state, request)['ok']
            repaired = copy.deepcopy(state['plan']['cases'][0])
            repaired['journey']['url'] = original['journey']['url']
            assert repaired == original
            result = await runner.run_journey(state, {'case_id': target.id})
            assert result['result']['status'] == 'passed', result
            assert actor.sessions == 1
            assert any(e['tool'] == 'entry_readiness' and e['ok'] for e in artifacts.observations)
            assert not runner.repair_journey_entry(state, request)['ok']
        finally:
            await engine.close()

    asyncio.run(run())


def test_exhausted_entry_readiness_exports_screenshot_and_not_run_checks(tmp_path, web_app):
    artifacts = Artifacts(tmp_path / 'runs')
    target = case(web_app)
    target.journey.url = web_app
    target.journey.entry = entry(artifacts)
    actor = Actor()
    engine = LocalTools(tmp_path, artifacts, headless=True)
    runner = VerificationRunner(None, engine, artifacts, journey_executor=actor, progress=lambda _: None)
    state = {'stage': 'execute', 'plan': {'cases': [target.model_dump()]}, 'findings': []}

    async def run():
        try:
            for _ in range(2):
                assert not (await runner.run_journey(state, {'case_id': target.id}))['ok']
            receipt = await runner.run_journey(state, {'case_id': target.id})
            assert receipt['ok'] and receipt['result']['status'] == 'blocked', receipt
            assert actor.sessions == 0
            result = runner.test_results[0]
            assert result.screenshots
            assert all((artifacts.path / path).exists() for path in result.screenshots)
            assert result.checkpoints[0].code == 'ENTRY_NOT_READY'
            assert all(c.code == 'NOT_RUN' for c in result.checkpoints[1:])
            assert 'Journey entry unavailable' in result.detail
            exported = json.loads((artifacts.path / result.test_file).with_suffix('.json').read_text())
            assert [s['kind'] for s in exported['steps']] == ['requires_verification']
            assert not (await runner.run_journey(state, {'case_id': target.id}))['ok']
            assert len([e for e in artifacts.observations if e['tool'] == 'setup_readiness']) == 3
        finally:
            await engine.close()

    asyncio.run(run())


def test_actual_session_entry_blocker_survives_inconclusive_health_and_caption(tmp_path, web_app):
    from open_verify.journey import JourneyRunner
    from open_verify.journey_spec import HealthJudgment
    from open_verify.reporting import evidence_caption

    artifacts = Artifacts(tmp_path / 'runs')
    target = case(web_app)
    target.journey.url = web_app
    target.journey.entry = entry(artifacts)

    class UncertainHealth(Actor):
        async def judge_health(self, instruction, observation, *, on_call):
            on_call()
            assert observation['journey_exercised'] is False
            return HealthJudgment(verdict='inconclusive', diagnosis='unknown',
                                  explanation='No completion or persistence evidence')

    actor = UncertainHealth()
    engine = LocalTools(tmp_path, artifacts, headless=True)
    async def run():
        try:
            return await JourneyRunner(engine, artifacts, actor, progress=lambda _: None).run(
                target, require_user_action=True)
        finally:
            await engine.close()
    result = asyncio.run(run())
    assert result.status == 'blocked' and 'Journey entry unavailable' in result.detail
    assert actor.sessions == 0
    points = [p.model_dump() for p in result.checkpoints]
    assert points[0]['code'] == 'ENTRY_NOT_READY'
    assert points[-1]['code'] == 'APP_HEALTH_INCONCLUSIVE'
    assert 'journey entry' in evidence_caption(result.detail, points, result.status)[0][1].lower()
