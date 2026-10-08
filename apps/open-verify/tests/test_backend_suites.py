"""Backend regression exports, actual engine evidence and standalone replay."""

import asyncio
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from test_change_workflow import impact
from test_workflow import ScriptedAgent, action

from open_verify.artifacts import Artifacts
from open_verify.backend_runner import BackendRunner, complete_text
from open_verify.backend_spec import BackendTest
from open_verify.changes import Change
from open_verify.models import Finding
from open_verify.runner import VerificationRunner
from open_verify.test_export import save_browser_test
from open_verify.test_spec import BrowserTest
from open_verify.tools import LocalTools


@pytest.fixture
def api():
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.command, self.path))
            body, status = b'{"items":[{"ok":true}],"empty":null}', 200
            if self.path == '/redirect':
                status = 302
            elif self.path == '/bad-json':
                body = b'not json'
            elif self.path == '/long':
                body = b'x' * 30_000 + b'TAIL'
            elif self.path == '/huge':
                body = b'x' * 1_000_001
            elif self.path == '/binary':
                body = b'\xff'
            self.send_response(status)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('X-Test', 'yes')
            if status == 302:
                self.send_header('Location', '/destination')
            self.end_headers()
            self.wfile.write(body)
        def do_POST(self):
            requests.append((self.command, self.path))
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            self.send_response(201)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}', requests
    server.shutdown()
    server.server_close()
    thread.join()


def suite(interface, steps, **kwargs):
    return BackendTest(case_id='backend', interface=interface, steps=steps,
                       checks={'Expected behavior': list(range(len(steps)))}, **kwargs)


def request(url, **expect):
    return {'kind': 'http', 'url': url, 'expect': {'status': 200, **expect}}


def command(code='print("hello")', **expect):
    return {'kind': 'command', 'argv': [sys.executable, '-c', code],
            'expect': {'exit_code': 0, **expect}}


def execute(tmp_path, test, *, allow_exec=True):
    artifacts = Artifacts(tmp_path / 'runs')
    engine = LocalTools(tmp_path, artifacts, allow_exec=allow_exec)
    async def run():
        try:
            return await BackendRunner(engine, artifacts, progress=lambda _: None).run(test)
        finally:
            assert await engine.close() == []
    return asyncio.run(run()), artifacts


def replay(result, artifacts, *, extra=()):
    return subprocess.run([sys.executable, *result.rerun[1:], *extra], cwd=artifacts.path,
        env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')},
        capture_output=True, text=True, timeout=30)


def planned(interface):
    return {'kind': 'plan', 'plan': {'project_summary': 'Backend fixture', 'startup': [],
        'cases': [{'id': 'backend', 'title': 'Backend behavior', 'interface': interface,
                   'steps': ['Run and check'], 'expected': 'Expected behavior'}]}}


def test_http_suite_exports_and_replays_real_response_assertions(tmp_path, api):
    url, requests = api
    test = suite('http', [request(url, headers={'x-test': 'yes'},
        json_check={'field': ['items', 0, 'ok'], 'value': True})])
    result, artifacts = execute(tmp_path, test)
    assert result.status == 'passed' and result.runner == 'http'
    assert replay(result, artifacts).returncode == 0
    assert len(requests) == 2
    assert 'open-verify[browser]' not in (artifacts.path / 'tests/requirements.txt').read_text()
    receipt = json.loads(next(artifacts.path.glob('backend-*.json')).read_text())
    assert receipt['steps'][0]['evidence']
    assert any(e['tool'] == 'backend_assert' and e['ok'] for e in artifacts.observations)


def test_terminal_expected_nonzero_exit_and_combined_output_replay(tmp_path):
    test = suite('terminal', [command('import sys; print(input()); print("err",file=sys.stderr); sys.exit(7)',
        exit_code=7, output={'mode': 'contains', 'value': 'Ada'})])
    test.steps[0].stdin = 'Ada\n'
    result, artifacts = execute(tmp_path, test)
    assert result.status == 'passed'
    assert replay(result, artifacts).returncode == 0
    assert not (tmp_path / '.git').exists()


def test_generated_helper_replays_after_original_script_and_checkout_are_removed(tmp_path):
    project = tmp_path / 'checkout'
    project.mkdir()
    artifacts = Artifacts(tmp_path / 'runs')
    engine = LocalTools(project, artifacts, allow_exec=True)
    # The helper invokes itself in a fresh process and uses both argv and stdin.
    source = ('import pathlib, subprocess, sys\n'
              'if len(sys.argv) == 1:\n'
              '    subprocess.run([sys.executable, __file__, "child"], check=True)\n'
              'else:\n'
              '    assert input() == "dummy"\n'
              '    assert pathlib.Path.cwd().name in {"checkout", "replacement"}\n'
              '    print("DURABLE_HELPER_PASS")\n')
    helper = project / '.open_verify' / 'lifecycle.py'
    async def run():
        await engine.execute('run_command', {'argv': [sys.executable, '-c',
            'import pathlib,sys; p=pathlib.Path(".open_verify/lifecycle.py"); '
            'p.parent.mkdir(); p.write_text(sys.stdin.read())'], 'stdin': source})
        test = suite('terminal', [{'kind': 'command', 'argv': [sys.executable, str(helper)],
            'stdin': 'dummy\n', 'expect': {'exit_code': 0,
            'output': {'mode': 'contains', 'value': 'DURABLE_HELPER_PASS'}}}])
        try:
            return await BackendRunner(engine, artifacts, progress=lambda _: None).run(test)
        finally:
            await engine.close()
    result = asyncio.run(run())
    assert result.status == 'passed'
    helper.unlink()
    helper.parent.rmdir()
    project.rmdir()
    replacement = tmp_path / 'replacement'
    replacement.mkdir()
    rerun = replay(result, artifacts, extra=('--project', str(replacement)))
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    exported = json.loads((artifacts.path / result.test_file).with_suffix('.json').read_text())
    assert exported['steps'][0]['argv'][1] == '-c'


def test_product_scripts_are_not_copied_without_matching_setup_evidence(tmp_path):
    from open_verify.backend_runner import embed_generated_scripts
    artifacts = Artifacts(tmp_path / 'runs')
    engine = LocalTools(tmp_path, artifacts, allow_exec=True)
    (tmp_path / 'product.py').write_text('print("product")')
    test = suite('terminal', [{'kind': 'command', 'argv': [sys.executable, 'product.py'],
                              'expect': {'exit_code': 0}}])
    assert embed_generated_scripts(test, engine, artifacts) == test


@pytest.mark.parametrize('expect', [{'status': 404}, {'headers': {'X-Test': 'no'}},
    {'json_check': {'field': ['missing'], 'value': None}},
    {'json_check': {'field': ['items', 0, 'ok'], 'value': 1}},
    {'json_check': {'field': ['items'], 'value': [{'ok': 1}]}},
    {'text': {'value': 'wrong'}}])
def test_http_assertion_failure_is_authoritative_and_replay_fails(tmp_path, api, expect):
    result, artifacts = execute(tmp_path, suite('http', [request(api[0], **expect)]))
    assert result.status == 'failed'
    assert replay(result, artifacts).returncode == 1


def test_null_json_and_redirect_response_are_checked_without_following(tmp_path, api):
    url, calls = api
    result, _ = execute(tmp_path, suite('http', [request(url, json_check={'field': ['empty'], 'value': None}),
        request(url + '/redirect', status=302, headers={'Location': '/destination'})]))
    assert result.status == 'passed'
    assert all(path != '/destination' for _, path in calls)


def test_post_body_is_literal_data_in_saved_source(tmp_path, api):
    payload = "'); __import__('os').system('touch UNWANTED'); #\n${HOME}`uname`"
    step = request(api[0], status=201, text={'value': payload})
    step.update(method='POST', body=payload)
    result, artifacts = execute(tmp_path, suite('http', [step]))
    assert result.status == 'passed' and replay(result, artifacts).returncode == 0
    assert not (tmp_path / 'UNWANTED').exists() and not (artifacts.path / 'UNWANTED').exists()


@pytest.mark.parametrize(('path', 'expected'), [('/long', 'passed'), ('/huge', 'blocked'), ('/binary', 'blocked')])
def test_text_assertions_use_complete_bounded_utf8_evidence(tmp_path, api, path, expected):
    result, _ = execute(tmp_path, suite('http', [request(api[0] + path, text={'mode': 'contains', 'value': 'TAIL'})]))
    assert result.status == expected, result.detail


def test_invalid_json_is_an_assertion_failure(tmp_path, api):
    result, _ = execute(tmp_path, suite('http', [request(api[0] + '/bad-json', json_check={'value': {}})]))
    assert result.status == 'failed'


def test_failed_step_stops_before_later_side_effects(tmp_path):
    result, artifacts = execute(tmp_path, suite('terminal', [command('raise SystemExit(2)'),
        command('from pathlib import Path; Path("unwanted").write_text("side effect")')]))
    assert result.status == 'failed' and not (tmp_path / 'unwanted').exists()
    report = json.loads(next(artifacts.path.glob('backend-*.json')).read_text())
    assert [s['status'] for s in report['steps']] == ['failed', 'blocked']


def test_command_permission_and_external_origin_refused_before_execution(tmp_path, api):
    result, artifacts = execute(tmp_path, suite('terminal', [command()]), allow_exec=False)
    assert result.status == 'blocked'
    assert not any(e['tool'] == 'run_command' for e in artifacts.observations)
    result, _ = execute(tmp_path, suite('http', [request(api[0]), request('https://example.com')]))
    assert result.status == 'blocked' and api[1] == []


def test_command_timeout_and_unsafe_cwd_block(tmp_path):
    step = command('import time; time.sleep(20)')
    step['timeout'] = .05
    result, _ = execute(tmp_path, suite('terminal', [step]))
    assert result.status == 'blocked' and 'deadline' in result.detail
    step = command()
    step['cwd'] = '../'
    result, _ = execute(tmp_path, suite('terminal', [step]))
    assert result.status == 'blocked'


def test_suite_deadline_and_cancel_keep_saved_test_and_partial_receipts(tmp_path):
    for cancel in (False, True):
        artifacts = Artifacts(tmp_path / 'runs')
        engine = LocalTools(tmp_path, artifacts, allow_exec=True)
        test = suite('terminal', [command('import time; time.sleep(20)')], timeout=30 if cancel else .08)
        saved = []
        async def run(engine=engine, artifacts=artifacts, test=test, saved=saved, cancel=cancel):
            try:
                task = asyncio.create_task(BackendRunner(engine, artifacts, progress=lambda _: None).run(test, on_result=saved.append))
                if cancel:
                    while not engine.processes:
                        await asyncio.sleep(.01)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    assert (await task).status == 'blocked'
            finally:
                assert await engine.close() == []
        asyncio.run(run())
        assert saved[-1].status == 'blocked'
        assert (artifacts.path / saved[-1].test_file).exists()
        report = json.loads(next(artifacts.path.glob('backend-*.json')).read_text())
        assert report['status'] == 'blocked'


@pytest.mark.parametrize('change', [False, True])
def test_workflow_finishes_from_backend_result_and_publishes_test(tmp_path, api, change):
    test = suite('http', [request(api[0])])
    assessment = impact(ui=False)
    assessment['impact']['affected_files'] = ['api.py']
    decisions = ([assessment] if change else []) + [planned('http'), action('run_backend_test', **test.model_dump())]
    artifacts = Artifacts(tmp_path / 'runs')
    runner = VerificationRunner(ScriptedAgent(decisions), LocalTools(tmp_path, artifacts), artifacts,
        change=Change(base='base', head='head', files=['api.py']) if change else None, progress=lambda _: None)
    report = asyncio.run(runner.run('Verify API'))
    assert report['findings'][0]['status'] == 'passed'
    manifest = json.loads((artifacts.path / 'manifest.json').read_text())
    assert manifest['schema_version'] == 1 and manifest['status'] == 'passed'
    assert manifest['tests'][0]['runner'] == 'http'
    assert [a['type'] for a in manifest['artifacts']] == ['test']
    assert 'tests/requirements.txt' in manifest['support_files']


def test_failed_backend_test_finishes_without_actor_override_or_retry(tmp_path, api):
    test = suite('http', [request(api[0], status=500)])
    artifacts = Artifacts(tmp_path / 'runs')
    runner = VerificationRunner(ScriptedAgent([planned('http'), action('run_backend_test', **test.model_dump())]),
        LocalTools(tmp_path, artifacts), artifacts, progress=lambda _: None)
    report = asyncio.run(runner.run('Verify API'))
    assert report['status'] == 'complete' and report['findings'][0]['status'] == 'failed'
    assert len(runner.test_results) == 1 and len(api[1]) == 1


def test_backend_policy_checks_ownership_coverage_and_one_execution(tmp_path, api):
    artifacts = Artifacts(tmp_path / 'runs')
    runner = VerificationRunner(None, LocalTools(tmp_path, artifacts), artifacts, progress=lambda _: None)
    current = planned('http')['plan']
    current['cases'][0]['checks'] = ['Expected behavior']
    state = {'stage': 'execute', 'plan': current, 'findings': []}
    valid = suite('http', [request(api[0])]).model_dump()
    async def run():
        for bad in ({**valid, 'case_id': 'someone-else'}, {**valid, 'checks': {}},
                    {**valid, 'checks': {'Renamed': [0]}}):
            assert not (await runner.run_backend_test(state, bad))['ok']
        assert not (await runner.run_backend_test({**state, 'stage': 'discover'}, valid))['ok']
        assert api[1] == []
        assert (await runner.run_backend_test(state, valid))['ok']
        assert not (await runner.run_backend_test(state, valid))['ok']
    asyncio.run(run())
    assert len(api[1]) == 1


def test_change_mode_cannot_accept_exploratory_backend_findings(tmp_path):
    artifacts = Artifacts(tmp_path / 'runs')
    runner = VerificationRunner(None, LocalTools(tmp_path, artifacts), artifacts,
        change=Change(base='b', head='h', files=['api.py']), progress=lambda _: None)
    e = artifacts.record('http_request', {}, {'status': 200}, True)
    state = {'plan': planned('http')['plan'], 'findings': []}
    finding = Finding(case_id='backend', status='passed', actual='Model claims success', evidence=[e['id']])
    assert 'latest generated test' in runner.validate_finding(state, finding)


@pytest.mark.parametrize('mutation', [
    {'steps': []}, {'interface': 'terminal'}, {'checks': {'Check': [3]}},
    {'checks': {'Check': [True]}}, {'checks': {'Check': [-1]}},
])
def test_invalid_backend_contracts_are_closed(mutation):
    value = suite('http', [request('http://localhost')]).model_dump()
    with pytest.raises(ValueError):
        BackendTest.model_validate({**value, **mutation})


@pytest.mark.parametrize('step', [
    {**request('http://localhost'), 'selector': 'x'}, request('http://user:pass@localhost'),
    request('file:///tmp/x'), {**request('http://localhost'), 'headers': {'Authorization': 'secret'}},
    request('http://localhost', json_check={'field': [-1], 'value': None}),
    request('http://localhost', json_check={'value': float('nan')}),
    request('http://localhost', text={'mode': 'contains', 'value': ''}),
])
def test_unsafe_or_ambiguous_contract_values_are_rejected(step):
    with pytest.raises(ValueError):
        suite('http', [step])


def test_evidence_path_escape_and_symlinks_are_rejected(tmp_path):
    outside = tmp_path / 'outside'
    outside.write_text('secret')
    root = tmp_path / 'artifacts'
    root.mkdir()
    (root / 'link').symlink_to(outside)
    for name in ('../outside', str(outside), 'link', 'missing'):
        with pytest.raises(ValueError):
            complete_text(root, {'log': name}, 'log')


@pytest.mark.parametrize('backend_first', [True, False])
def test_mixed_bundle_keeps_both_replay_instructions(tmp_path, api, backend_first):
    artifacts = Artifacts(tmp_path / 'runs')
    browser = BrowserTest(case_id='ui', url=api[0], steps=[{'kind': 'expect_text', 'text': 'Expected'}])
    from open_verify.backend_runner import save_backend_test
    backend = suite('http', [request(api[0])])
    if backend_first:
        save_backend_test(backend, artifacts)
    save_browser_test(browser, artifacts, attempt=1)
    if not backend_first:
        save_backend_test(backend, artifacts)
    assert 'open-verify[browser]' in (artifacts.path / 'tests/requirements.txt').read_text()
    text = (artifacts.path / 'tests/README.md').read_text()
    assert 'Generated Playwright' in text and 'Generated HTTP and terminal' in text



def checkpoint_suite(code, names):
    return BackendTest(case_id='backend', interface='terminal',
        steps=[command(code, checkpoints=names)], checks={name: [0] for name in names})


def test_named_checkpoints_survive_later_command_failure_and_replay(tmp_path):
    names = ['Empty queue', 'Approved tickets queued', 'Queue survives restart']
    code = """import json
queue = []
assert queue == []
print('OV_CHECKPOINT ' + json.dumps({'check': 'Empty queue', 'detail': 'queue=[]'}), flush=True)
queue.append('rejected-ticket')
assert queue == ['approved-ticket'], 'Rejected ticket appeared in queue'
print('OV_CHECKPOINT ' + json.dumps({'check': 'Approved tickets queued', 'detail': str(queue)}), flush=True)
"""
    result, artifacts = execute(tmp_path, checkpoint_suite(code, names))
    assert result.status == 'failed'
    assert [p.status for p in result.checkpoints] == ['passed', 'failed', 'blocked']
    assert result.checkpoints[0].detail == 'queue=[]'
    assert result.checkpoints[-1].code == 'NOT_RUN'
    persisted = json.loads(next(artifacts.path.glob('backend-*.json')).read_text())
    assert persisted['checkpoints'] == [p.model_dump(exclude_none=True) for p in result.checkpoints]
    replayed = replay(result, artifacts)
    assert replayed.returncode == 1, replayed.stdout + replayed.stderr
    replay_result = json.loads(next((artifacts.path / 'verification-replay').rglob('backend-*.json')).read_text())
    assert replay_result['checkpoints'] == persisted['checkpoints']


def test_named_checkpoint_details_reach_manifest_and_report(tmp_path):
    names = ['Queue before approval', 'Queue after approval']
    code = """import json
queue = []
assert not queue
print('OV_CHECKPOINT ' + json.dumps({'check': 'Queue before approval', 'detail': 'queue=[]'}), flush=True)
queue.append('approved-ticket')
assert queue == ['approved-ticket']
print('OV_CHECKPOINT ' + json.dumps({'check': 'Queue after approval', 'detail': 'queue=[approved-ticket]'}), flush=True)
"""
    test = checkpoint_suite(code, names)
    plan = planned('terminal')
    plan['plan']['cases'][0]['checks'] = names
    artifacts = Artifacts(tmp_path / 'runs')
    engine = LocalTools(tmp_path, artifacts, allow_exec=True)
    progress = []
    runner = VerificationRunner(ScriptedAgent([plan, action('run_backend_test', **test.model_dump())]),
        engine, artifacts, progress=progress.append)
    report = asyncio.run(runner.run('Check approval queue'))
    assert report['findings'][0]['status'] == 'passed'
    manifest = json.loads((artifacts.path / 'manifest.json').read_text())
    assert [p['instruction'] for p in manifest['tests'][0]['checkpoints']] == names
    assert all(p['status'] == 'passed' for p in manifest['tests'][0]['checkpoints'])
    assert any('queue=[approved-ticket]' in line for line in progress)
    visible, details = (artifacts.path / 'report.md').read_text().split('<details>', 1)
    assert names[-1] in visible and '2 checks.' in visible
    assert all(name in details for name in names)
    assert 'queue=[approved-ticket]' in details


@pytest.mark.parametrize('output', [
    'OV_CHECKPOINT not-json',
    'OV_CHECKPOINT {"check":"Unexpected","detail":"bad"}',
    'OV_CHECKPOINT {"check":"Second","detail":"out of order"}',
    'OV_CHECKPOINT {"check":"First","detail":"ok"}\nOV_CHECKPOINT {"check":"First","detail":"duplicate"}',
    'OV_CHECKPOINT {"check":"First","detail":"ok","status":"passed"}',
    '',
])
def test_invalid_or_missing_checkpoint_events_cannot_pass(tmp_path, output):
    result, _ = execute(tmp_path, checkpoint_suite(f'print({output!r})', ['First', 'Second']))
    assert result.status == 'blocked'
    assert not all(p.status == 'passed' for p in result.checkpoints)


def test_failure_after_all_checkpoints_remains_visible(tmp_path):
    output = 'OV_CHECKPOINT {"check":"Only check","detail":"observed"}'
    result, _ = execute(tmp_path, checkpoint_suite(f'print({output!r}); raise SystemExit(1)', ['Only check']))
    assert result.status == 'failed'
    assert [p.status for p in result.checkpoints] == ['passed', 'failed']
    assert result.checkpoints[-1].instruction == 'Backend execution completed'


@pytest.mark.parametrize('names,checks', [
    (['One', 'One'], {'One': [0]}),
    (['Unknown'], {'One': [0]}),
    (['One'], {'One': [0], 'Two': [0]}),
])
def test_checkpoint_declarations_must_match_planned_checks(names, checks):
    with pytest.raises(ValueError):
        BackendTest(case_id='backend', interface='terminal', steps=[command(checkpoints=names)], checks=checks)


def test_post_checkpoint_failure_does_not_disappear_before_unrun_command(tmp_path):
    output = 'OV_CHECKPOINT {"check":"First","detail":"observed"}'
    test = BackendTest(case_id='backend', interface='terminal', steps=[
        command(f'print({output!r}); raise SystemExit(1)', checkpoints=['First']),
        command('raise AssertionError("Must not execute")', checkpoints=['Second']),
    ], checks={'First': [0], 'Second': [1]})
    result, _ = execute(tmp_path, test)
    assert result.status == 'failed'
    assert [p.status for p in result.checkpoints] == ['passed', 'blocked', 'failed']
    assert result.checkpoints[1].code == 'NOT_RUN'
    assert result.checkpoints[2].instruction == 'Backend execution completed'


def python_harness_suite(source, names=('First', 'Second'), **kwargs):
    return BackendTest(case_id='backend', interface='terminal', steps=[{
        'kind': 'command', 'argv': [sys.executable, '-'], 'stdin': source,
        'expect': {'exit_code': 0, 'python_harness': True, 'checkpoints': list(names)},
    }], checks={name: [0] for name in names}, **kwargs)


ENUM_FIXTURE_ERROR = """import json
from dataclasses import dataclass, asdict
from enum import Enum
class TicketLayer(Enum):
    API = 'api'
@dataclass
class Ticket:
    layer: TicketLayer
json.dumps(asdict(Ticket(TicketLayer.API)))
"""


GOOD_HARNESS = """import json
from enum import Enum
class TicketLayer(Enum):
    API = 'api'
payload = json.dumps({'layer': TicketLayer.API.value})
assert json.loads(payload) == {'layer': 'api'}
print('OV_CHECKPOINT ' + json.dumps({'check': 'First', 'detail': 'layer=api'}), flush=True)
assert True
print('OV_CHECKPOINT ' + json.dumps({'check': 'Second', 'detail': 'queue=[]'}), flush=True)
"""


def test_enum_serialization_harness_error_is_blocked_with_exception_and_replays(tmp_path):
    result, artifacts = execute(tmp_path, python_harness_suite(ENUM_FIXTURE_ERROR))
    assert result.status == 'blocked'
    assert 'TypeError: Object of type TicketLayer is not JSON serializable' in result.detail
    assert result.checkpoints[0].code == 'HARNESS_ERROR'
    assert result.checkpoints[1].code == 'NOT_RUN'
    assert not any(p.status == 'failed' for p in result.checkpoints)
    replayed = replay(result, artifacts)
    assert replayed.returncode == 2, replayed.stdout + replayed.stderr
    assert 'TicketLayer is not JSON serializable' in replayed.stdout


def test_python_harness_assertion_is_failed_and_shows_actual_assertion(tmp_path):
    result, _ = execute(tmp_path, python_harness_suite('assert False, "Rejected ticket appeared in queue"'))
    assert result.status == 'failed'
    assert result.detail == 'AssertionError: Rejected ticket appeared in queue'
    assert not any(p.code == 'HARNESS_ERROR' for p in result.checkpoints)


def test_python_harness_partial_success_survives_runtime_error(tmp_path):
    source = GOOD_HARNESS.split('assert True')[0] + 'raise ValueError("broken fixture")'
    result, _ = execute(tmp_path, python_harness_suite(source))
    assert result.status == 'blocked'
    assert [p.status for p in result.checkpoints] == ['passed', 'blocked']
    assert result.checkpoints[1].code == 'HARNESS_ERROR'


@pytest.mark.parametrize('source', ['raise SystemExit(3)', 'import os; os._exit(3)'])
def test_python_harness_unexplained_exit_cannot_be_product_failure(tmp_path, source):
    result, _ = execute(tmp_path, python_harness_suite(source))
    assert result.status == 'blocked'
    assert result.checkpoints[0].code == 'HARNESS_ERROR'


def test_classified_harness_error_gets_one_repair_and_preserves_both_attempts(tmp_path):
    first = python_harness_suite(ENUM_FIXTURE_ERROR)
    second = python_harness_suite(GOOD_HARNESS,
        retry_reason='asdict retained TicketLayer; serialize its .value in the mock provider response')
    plan = planned('terminal')
    plan['plan']['cases'][0]['checks'] = list(first.checks)
    agent = ScriptedAgent([plan, action('run_backend_test', **first.model_dump()),
        action('run_backend_test', **second.model_dump())])
    artifacts = Artifacts(tmp_path / 'runs')
    runner = VerificationRunner(agent, LocalTools(tmp_path, artifacts, allow_exec=True),
        artifacts, progress=lambda _: None)
    report = asyncio.run(runner.run('Check queue'))
    assert report['findings'][0]['status'] == 'passed'
    assert [r.status for r in runner.test_results] == ['blocked', 'passed']
    files = [artifacts.path / r.test_file for r in runner.test_results]
    assert files[0] != files[1] and all(p.exists() for p in files)
    retained = [json.loads(p.with_suffix('.result.json').read_text()) for p in files]
    assert [r['status'] for r in retained] == ['blocked', 'passed']
    assert retained[0]['checkpoints'][0]['code'] == 'HARNESS_ERROR'
    manifest = json.loads((artifacts.path / 'manifest.json').read_text())
    assert manifest['tests'][0]['status'] == 'passed'


def test_harness_repair_budget_stops_after_second_crash(tmp_path):
    first = python_harness_suite(ENUM_FIXTURE_ERROR)
    second = python_harness_suite('raise ValueError("still broken")', retry_reason='Correct the enum fixture')
    plan = planned('terminal')
    plan['plan']['cases'][0]['checks'] = list(first.checks)
    agent = ScriptedAgent([plan, action('run_backend_test', **first.model_dump()),
        action('run_backend_test', **second.model_dump())])
    artifacts = Artifacts(tmp_path / 'runs')
    runner = VerificationRunner(agent, LocalTools(tmp_path, artifacts, allow_exec=True),
        artifacts, progress=lambda _: None)
    report = asyncio.run(runner.run('Check queue'))
    assert report['findings'][0]['status'] == 'blocked'
    assert 'ValueError: still broken' in report['findings'][0]['actual']
    assert len(runner.test_results) == 2


@pytest.mark.parametrize('invalid', ['no_reason', 'unchanged', 'weakened_exit', 'changed_timeout'])
def test_harness_repair_requires_diagnosis_and_fixed_expectations(tmp_path, invalid):
    first = python_harness_suite(ENUM_FIXTURE_ERROR)
    second = python_harness_suite(GOOD_HARNESS, retry_reason='Serialize enum .value')
    bad = second.model_dump()
    if invalid == 'no_reason':
        bad['retry_reason'] = ''
    elif invalid == 'unchanged':
        bad['steps'][0]['stdin'] = ENUM_FIXTURE_ERROR
    elif invalid == 'changed_timeout':
        bad['timeout'] = 120
    else:
        bad['steps'][0]['expect']['python_harness'] = False
        bad['steps'][0]['expect']['exit_code'] = 2
    plan = planned('terminal')
    plan['plan']['cases'][0]['checks'] = list(first.checks)
    agent = ScriptedAgent([plan, action('run_backend_test', **first.model_dump()),
        action('run_backend_test', **bad), action('run_backend_test', **second.model_dump())])
    artifacts = Artifacts(tmp_path / 'runs')
    runner = VerificationRunner(agent, LocalTools(tmp_path, artifacts, allow_exec=True),
        artifacts, progress=lambda _: None)
    report = asyncio.run(runner.run('Check queue'))
    assert report['findings'][0]['status'] == 'passed'
    assert len(runner.test_results) == 2
    assert any(e['tool'] == 'run_backend_test' and not e['ok'] for e in artifacts.observations)


def test_python_assertion_failure_is_final_without_repair(tmp_path):
    test = python_harness_suite('assert False, "Observed wrong queue"')
    plan = planned('terminal')
    plan['plan']['cases'][0]['checks'] = list(test.checks)
    artifacts = Artifacts(tmp_path / 'runs')
    agent = ScriptedAgent([plan, action('run_backend_test', **test.model_dump())])
    runner = VerificationRunner(agent, LocalTools(tmp_path, artifacts, allow_exec=True),
        artifacts, progress=lambda _: None)
    report = asyncio.run(runner.run('Check queue'))
    assert report['findings'][0]['status'] == 'failed'
    assert len(runner.test_results) == 1


@pytest.mark.parametrize('bad', [
    {'argv': [sys.executable, '-c', 'pass']},
    {'argv': ['sh', '-']},
    {'stdin': ''},
    {'expect': {'exit_code': 1, 'python_harness': True}},
])
def test_python_harness_mode_requires_python_stdin_and_zero_exit(bad):
    value = python_harness_suite(GOOD_HARNESS).model_dump()
    value['steps'][0].update(bad)
    with pytest.raises(ValueError):
        BackendTest.model_validate(value)


def test_python_harness_preserves_main_module_and_standard_input_identity(tmp_path):
    source = """import pickle, sys
from dataclasses import dataclass
@dataclass
class Sample:
    name: str
assert pickle.loads(pickle.dumps(Sample('ticket'))).name == 'ticket'
assert sys.argv == ['-']
assert __name__ == '__main__' and __file__ == '<stdin>'
print('OV_CHECKPOINT {"check":"First","detail":"pickle round trip passed"}', flush=True)
"""
    result, _ = execute(tmp_path, python_harness_suite(source, names=('First',)))
    assert result.status == 'passed', result.detail
