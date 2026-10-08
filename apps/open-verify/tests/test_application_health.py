"""Narrow UI assertions cannot hide unexpected workflow or managed-server failures."""

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest
from test_application_qa import web_manifest
from test_journeys import Actor, complete
from test_workflow import ScriptedAgent, action

from open_verify.artifacts import Artifacts
from open_verify.health import ApplicationHealth
from open_verify.journey import JourneyRunner
from open_verify.journey_spec import ActDecision, HealthJudgment, Judgment
from open_verify.models import Case
from open_verify.runner import VerificationRunner
from open_verify.tools import LocalTools

SERVER = r'''
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
mode = sys.argv[1]
error = 'the implementation agent ended 3 turns without reporting a valid terminal result'
class App(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/create':
            if mode in {'broken', 'log_error'}:
                print('naming response must end with a JSON object containing a name', flush=True)
                print('RuntimeError: ' + error, flush=True)
            body = b'ok'
        elif self.path == '/runs/new':
            body = b"""<h1>Create a WorkOrder</h1><label>Task prompt<textarea id='prompt'></textarea></label>
            <button onclick="localStorage.setItem('prompt', document.querySelector('#prompt').value);
            fetch('/create').then(() => location.href='/runs/dummy')">Create WorkOrder</button>"""
        else:
            state = 'failed' if mode in {'broken', 'expected_error'} else 'awaiting human review'
            message = error if mode == 'broken' else 'Expected rejection' if mode == 'expected_error' else ''
            body = ("<h1>WorkOrder detail</h1><p id='prompt'></p><strong>" + state +
                "</strong><p>/tmp/.ov-qa/repository</p><p role='alert'>" + message + "</p><script>document.querySelector('#prompt').textContent=localStorage.getItem('prompt')</script>").encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *_): pass
server = ThreadingHTTPServer(('127.0.0.1', 0), App)
print('Old unrelated error from startup diagnostics', flush=True)
print('READY=http://127.0.0.1:' + str(server.server_port), flush=True)
server.serve_forever()
'''


def workorder(url, *, negative=False):
    return Case.model_validate({'id': 'create', 'title': 'Create and retain a WorkOrder',
        'interface': 'browser', 'coverage': 'regression',
        'steps': ['Create', 'Inspect', 'Reload'],
        'expected': ('Explicitly test failure handling: show failed and Expected rejection' if negative
                     else 'Create a healthy WorkOrder awaiting human review and retain it after reload'),
        'journey': {'url': url + '/runs/new', 'steps': [
            {'kind': 'assert', 'instruction': 'Creation form visible',
             'check': {'kind': 'expect_text', 'text': 'Create a WorkOrder'}},
            {'kind': 'act', 'instruction': 'Enter OV dummy and create the WorkOrder'},
            {'kind': 'assert', 'instruction': 'Submitted prompt visible',
             'check': {'kind': 'expect_text', 'text': 'OV dummy'}, 'remember_as': 'created'},
            {'kind': 'act', 'instruction': 'Reload the created detail'},
            {'kind': 'assert', 'instruction': 'Same record after reload',
             'check': {'kind': 'expect_same_url', 'baseline': 'created'}},
            {'kind': 'assert', 'instruction': 'Submitted prompt survives reload',
             'check': {'kind': 'expect_text', 'text': 'OV dummy'}},
        ]}})


class ObservingActor(Actor):
    def __init__(self, mode):
        decisions = [ActDecision.model_validate({'kind': 'action', 'action': item}) for item in [
            {'tool': 'browser_fill', 'arguments': {'by': 'label', 'name': 'Task prompt', 'value': 'OV dummy'}, 'reason': 'Enter prompt'},
            {'tool': 'browser_click', 'arguments': {'by': 'role', 'role': 'button', 'name': 'Create WorkOrder'}, 'reason': 'Create'},
        ]]
        decisions += [complete(), ActDecision.model_validate({'kind': 'action', 'action': {
            'tool': 'browser_reload', 'arguments': {}, 'reason': 'Reload'}}), complete()]
        super().__init__(decisions)
        self.mode = mode
        self.health_observations = []

    async def judge(self, instruction, observation, *, on_call):
        on_call()
        self.health_observations.append(observation)
        assert 'actor' not in observation and 'completed_steps' not in observation
        assert observation['url'].endswith('/runs/dummy')
        assert 'OV dummy' in observation['snapshot']
        logs = '\n'.join(p['new_output'] for p in observation['server_logs'])
        assert 'Old unrelated error' not in logs
        if self.mode in {'broken', 'log_error'}:
            assert 'without reporting a valid terminal result' in logs
            if self.mode == 'broken':
                assert 'failed' in observation['snapshot']
            return HealthJudgment(verdict='fails', diagnosis='application', explanation='The scripted implementation did not report a valid terminal result.')
        if self.mode == 'expected_error':
            assert 'Explicitly test failure handling' in observation['expected']
            assert 'Expected rejection' in observation['snapshot']
        else:
            assert 'awaiting human review' in observation['snapshot']
        return Judgment(verdict='holds', explanation='The resulting state matches the expected user outcome; no new server errors.')


@pytest.mark.parametrize('mode,expected', [
    ('broken', 'blocked'), ('log_error', 'blocked'), ('healthy', 'passed'), ('expected_error', 'passed'),
])
def test_real_ui_prompt_and_reload_pass_but_unexpected_errors_cannot(tmp_path, mode, expected):
    pytest.importorskip('playwright.async_api')
    web_manifest(tmp_path)
    artifacts = Artifacts(tmp_path / 'runs')
    engine = LocalTools(tmp_path, artifacts, allow_exec=True, headless=True)
    observer = ObservingActor(mode)

    async def run():
        try:
            started = await engine.execute('start_process', {'argv': [sys.executable, '-u', '-c', SERVER, mode]})
            pid = started['result']['process_id']
            async with asyncio.timeout(10):
                while True:
                    output = await engine.execute('process_output', {'process_id': pid})
                    lines = output['result']['output'].splitlines()
                    ready = next((line for line in lines if line.startswith('READY=')), None)
                    if ready:
                        break
                    await asyncio.sleep(.01)
            target = workorder(ready.removeprefix('READY='), negative=mode == 'expected_error')
            receipt = artifacts.record('read_file', {'path': 'fixture-server.py'},
                {'text': 'The /runs/new route exposes the Create a WorkOrder form.'}, True)
            data = target.model_dump()
            data['journey']['entry'] = {'evidence': [receipt['id']], 'controls': [
                {'kind': 'assert', 'instruction': 'Creation form ready',
                 'check': {'kind': 'expect_text', 'text': 'Create a WorkOrder'}}]}
            target = Case.model_validate(data)
            plan = {'kind': 'plan', 'plan': {'project_summary': 'QA app', 'startup': [],
                'cases': [target.model_dump()]}}
            agent = ScriptedAgent([plan, action('run_journey', case_id=target.id)])
            runner = VerificationRunner(agent, engine, artifacts, journey_executor=observer, progress=lambda _: None)
            return await runner.run('Create and retain a healthy WorkOrder'), runner
        finally:
            await engine.close()

    report, runner = asyncio.run(run())
    assert report['findings'][0]['status'] == expected
    points = runner.test_results[0].checkpoints
    assert all(p.status == 'passed' for p in points[:-1])
    assert points[-1].status == expected
    assert points[-1].code == ('UNEXPECTED_APP_ERROR' if expected == 'blocked' else 'APP_HEALTH_OK')
    assert len(observer.health_observations) == 1
    assert runner.test_results[0].screenshots
    receipt = next(e for e in artifacts.observations if e['tool'] == 'application_health')
    assert (artifacts.path / receipt['artifact']).is_file()
    manifest = json.loads((artifacts.path / 'manifest.json').read_text())
    assert manifest['status'] == expected
    assert manifest['tests'][0]['checkpoints'][-1]['code'] == points[-1].code
    exported = json.loads((artifacts.path / runner.test_results[0].test_file).with_suffix('.json').read_text())
    assert any(s['kind'] == 'requires_verification' and 'independent observer' in s['reason'] for s in exported['steps'])


class EvidenceEngine:
    def __init__(self, artifacts, *, truncated=False, refuse=False):
        self.artifacts, self.truncated, self.refuse = artifacts, truncated, refuse
        self.calls = 0

    def environment(self):
        return {'managed_processes': [{'process_id': 'P001'}]}

    async def execute(self, tool, arguments):
        if tool == 'browser_snapshot':
            value = {'url': 'http://localhost/run', 'snapshot': 'WorkOrder awaiting human review', 'truncated': False}
        else:
            self.calls += 1
            value = {'output': 'Old error\n' + ('New output\n' if self.calls > 1 else ''),
                     'truncated': self.truncated, 'argv': ['server'], 'exit_code': None, 'log': 'P001.log'}
        return self.artifacts.record(tool, arguments, value, not self.refuse)


@pytest.mark.parametrize('options', [{'truncated': True}, {'refuse': True}])
def test_incomplete_evidence_cannot_pass_even_when_judge_says_healthy(tmp_path, options):
    artifacts = Artifacts(tmp_path / 'runs')
    engine = EvidenceEngine(artifacts, **options)
    judge = Actor()
    observer = ApplicationHealth(engine, artifacts, judge)
    async def run():
        return await observer.inspect(engine, workorder('http://localhost'), await observer.baseline())
    point = asyncio.run(run())
    assert point['status'] == 'blocked'
    assert point['code'] == 'APP_HEALTH_INCONCLUSIVE'
    assert engine.calls == 2


def test_missing_independent_observer_is_blocked_after_log_inspection(tmp_path):
    artifacts = Artifacts(tmp_path / 'runs')
    engine = EvidenceEngine(artifacts)
    observer = ApplicationHealth(engine, artifacts, None)
    async def run():
        return await observer.inspect(engine, workorder('http://localhost'), await observer.baseline())
    point = asyncio.run(run())
    assert point['status'] == 'blocked'
    assert 'observer is unavailable' in point['detail']
    assert engine.calls == 2


def test_observer_cannot_exceed_model_budget(tmp_path):
    artifacts = Artifacts(tmp_path / 'runs')
    engine = EvidenceEngine(artifacts)
    class TooMany:
        async def judge(self, *args, on_call):
            on_call()
            on_call()
            on_call()
            return Judgment(verdict='holds', explanation='Healthy')
    observer = ApplicationHealth(engine, artifacts, TooMany())
    point = asyncio.run(observer.inspect(engine, SimpleNamespace(expected='Healthy', checks=[]), {}))
    assert point['status'] == 'blocked' and point['model_calls'] == 2


@pytest.mark.parametrize('mode,diagnosis,expected,code', [
    ('broken', 'fixture', 'blocked', 'FIXTURE_ERROR'),
    ('healthy', 'assertion', 'blocked', 'ASSERTION_INVALID'),
    ('healthy', 'unknown', 'failed', 'APP_HEALTH_OK'),
])
def test_failed_check_still_collects_health_and_preserves_unrun_checks(tmp_path, mode, diagnosis, expected, code):
    artifacts = Artifacts(tmp_path / 'runs')
    engine = LocalTools(tmp_path, artifacts, allow_exec=True, headless=True)

    class DiagnosingActor(ObservingActor):
        async def judge_health(self, instruction, observation, *, on_call):
            # Check real screen/log evidence, not the actor's completion claims.
            original = await self.judge(instruction, observation, on_call=on_call)
            failure = observation['failed_checks'][0]
            assert failure['instruction'] == 'Repository is displayed'
            assert failure['check']['match'] == 'exact'
            assert failure['check']['text'] == '.ov-qa/repository'
            assert '/tmp/.ov-qa/repository' in observation['snapshot']
            return HealthJudgment(**original.model_dump(exclude={'diagnosis'}), diagnosis=diagnosis)

    async def run():
        try:
            started = await engine.execute('start_process', {'argv': [sys.executable, '-u', '-c', SERVER, mode]})
            async with asyncio.timeout(10):
                while True:
                    output = await engine.execute('process_output', {'process_id': started['result']['process_id']})
                    ready = next((line for line in output['result']['output'].splitlines() if line.startswith('READY=')), None)
                    if ready:
                        break
                    await asyncio.sleep(.01)
            target = workorder(ready.removeprefix('READY='))
            data = target.model_dump()
            data['journey']['steps'].insert(3, {'kind': 'assert', 'instruction': 'Repository is displayed',
                'check': {'kind': 'expect_text', 'text': '.ov-qa/repository'}, 'timeout': 10})
            target = Case.model_validate(data)
            result = await JourneyRunner(engine, artifacts, DiagnosingActor(mode),
                progress=lambda _: None).run(target, require_user_action=True)
            return result, target
        finally:
            await engine.close()

    result, target = asyncio.run(run())
    assert result.status == expected
    assert result.checkpoints[-1].code == code
    assert any(p.status == 'failed' and p.instruction == 'Repository is displayed' for p in result.checkpoints)
    assert sum(p.code == 'NOT_RUN' for p in result.checkpoints) == 2
    journey = json.loads(next((artifacts.path / 'journeys').glob('*.json')).read_text())
    assert len(journey['steps']) == len(target.journey.steps) + 1
    assert journey['steps'][4]['code'] == 'NOT_RUN'  # Reload never ran.
    assert any(e['tool'] == 'application_health' for e in artifacts.observations)


def test_health_diagnosis_transport_is_fresh_and_closed():
    from open_verify.step_executor import AgentJourneyExecutor

    class Transport:
        def __init__(self):
            self.resets = 0
        async def reset_session(self):
            self.resets += 1
        async def respond(self, prompt, schema, *, on_call):
            on_call()
            assert schema is HealthJudgment
            assert 'new server failure' in prompt and 'response_schema' in prompt
            return schema(verdict='fails', diagnosis='fixture', explanation='Scripted provider rejected the prompt')

    agent = Transport()
    response = asyncio.run(AgentJourneyExecutor(agent).judge_health('Health',
        {'server_logs': ['new server failure']}, on_call=lambda: None))
    assert agent.resets == 1 and response.diagnosis == 'fixture'


@pytest.mark.parametrize('stop', ['blocked', 'error', 'cancel'])
def test_failed_actor_still_gets_health_but_cancellation_stops_model_work(tmp_path, stop):
    from test_journeys import FakeEngine, case, read_steps

    class Stopped(Actor):
        async def act(self, context, *, on_call):
            on_call()
            if stop == 'cancel':
                raise asyncio.CancelledError()
            if stop == 'error':
                raise RuntimeError('Transport unavailable')
            return ActDecision(kind='complete', outcome='blocked', summary='Cannot continue')

    artifacts = Artifacts(tmp_path / 'runs')
    engine = FakeEngine(artifacts)
    actor = Stopped()
    pending = JourneyRunner(engine, artifacts, actor, progress=lambda _: None).run(
        case('http://localhost'), require_user_action=True)
    if stop == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(pending)
        assert not actor.judgments
        assert not any(e['tool'] == 'application_health' for e in artifacts.observations)
    else:
        result = asyncio.run(pending)
        assert result.status == 'blocked'  # A healthy screen never erases blocked actions.
        assert len(actor.judgments) == 1
        assert result.checkpoints[-1].code == 'APP_HEALTH_OK'
    assert read_steps(artifacts)[1]['code'] == 'NOT_RUN'
    assert engine.closed
