"""Manual QA covers the runnable app even when only backend packages change."""

import asyncio
import json
import sys

import pytest
from test_backend_suites import planned, python_harness_suite
from test_change_workflow import browser_plan, execute, impact
from test_journeys import Actor, case, complete
from test_tools import web_app as web_app
from test_workflow import ScriptedAgent, action

from open_verify.application import application_surface, inline_harness
from open_verify.artifacts import Artifacts
from open_verify.backend_spec import BackendTest
from open_verify.changes import Change
from open_verify.runner import VerificationRunner
from open_verify.tools import LocalTools


def web_manifest(root):
    folder = root / 'apps' / 'web'
    folder.mkdir(parents=True)
    (folder / 'package.json').write_text(json.dumps({'scripts': {'dev': 'vite --host'}}))



def smoke_plan():
    plan = browser_plan()
    plan['plan']['cases'][0]['journey'] = {'url': 'http://localhost', 'steps': [
        {'kind': 'act', 'instruction': 'Enter Ada and click Greet'},
        {'kind': 'assert', 'instruction': 'Greeting appears',
         'check': {'kind': 'expect_text', 'text': 'Hello Ada'}},
    ]}
    return plan


def cli_manifest(root):
    (root / 'pyproject.toml').write_text('[project.scripts]\nqa-app = "app:main"\n')


def test_discovers_whole_application_outside_changed_backend_package(tmp_path):
    cli_manifest(tmp_path)
    web_manifest(tmp_path)
    assert application_surface(tmp_path) == {
        'interface': 'browser', 'evidence': 'apps/web/package.json',
        'reason': 'Runnable web client declared in package scripts',
    }


def test_dependency_manifests_and_symlinks_do_not_create_app_requirement(tmp_path):
    dependency = tmp_path / 'node_modules' / 'demo'
    dependency.mkdir(parents=True)
    (dependency / 'package.json').write_text('{"scripts":{"dev":"vite"}}')
    (tmp_path / 'package.json').write_text('{"exports":"./index.js"}')
    (tmp_path / 'apps').mkdir()
    (tmp_path / 'apps' / 'linked').symlink_to(dependency, target_is_directory=True)
    assert application_surface(tmp_path) is None


def test_backend_only_diff_cannot_spend_only_case_on_library_in_web_app(tmp_path):
    web_manifest(tmp_path)
    library = planned('terminal')
    library['plan']['cases'][0]['interaction'] = 'library'
    _, browser, manifest, agent = execute(tmp_path, [
        impact(ui=False), library, smoke_plan(),
    ], plan_only=True)
    assert manifest['status'] == 'planned'
    assert browser.calls == []
    assert 'Manual QA requires a browser application smoke' in agent.prompts[2]
    assert 'apps/web/package.json' in agent.prompts[1]
    assert manifest['tests'] == []
    assert json.loads((next((tmp_path / 'runs').iterdir()) / 'plan.json').read_text())['cases'][0]['interface'] == 'browser'


def test_cli_library_plan_cannot_replace_user_command(tmp_path):
    cli_manifest(tmp_path)
    library = planned('terminal')
    library['plan']['cases'][0]['interaction'] = 'library'
    agent = ScriptedAgent([library, planned('terminal')])
    artifacts = Artifacts(tmp_path / 'runs')
    runner = VerificationRunner(agent, LocalTools(tmp_path, artifacts), artifacts,
        plan_only=True, progress=lambda _: None)
    report = asyncio.run(runner.run('Smoke the app'))
    assert report['status'] == 'planned'
    assert 'Manual QA requires a terminal application smoke' in agent.prompts[1]


def test_existing_tests_mode_does_not_require_manual_smoke(tmp_path):
    web_manifest(tmp_path)
    library = planned('terminal')
    library['plan']['cases'][0].update(interaction='library', verification='existing_tests')
    artifacts = Artifacts(tmp_path / 'runs')
    runner = VerificationRunner(ScriptedAgent([library]), LocalTools(tmp_path, artifacts), artifacts,
        plan_only=True, verification='tests', progress=lambda _: None)
    assert asyncio.run(runner.run('Recheck CI'))['status'] == 'planned'


def test_library_only_project_can_still_plan_public_api_coverage(tmp_path):
    plan = planned('terminal')
    plan['plan']['cases'][0]['interaction'] = 'library'
    artifacts = Artifacts(tmp_path / 'runs')
    runner = VerificationRunner(ScriptedAgent([plan]), LocalTools(tmp_path, artifacts), artifacts,
        plan_only=True, progress=lambda _: None)
    assert asyncio.run(runner.run('Check library API'))['status'] == 'planned'
    assert 'Supporting library check' in (artifacts.path / 'report.md').read_text()


def test_legacy_browser_case_cannot_bypass_independent_health_check(tmp_path):
    web_manifest(tmp_path)
    report, browser, _, agent = execute(tmp_path, [
        impact(ui=False), browser_plan(), smoke_plan(),
    ], plan_only=True)
    assert report['status'] == 'planned'
    assert browser.calls == []
    assert 'structured browser journey' in agent.prompts[2]


def test_structured_page_assertion_alone_cannot_be_application_smoke(tmp_path):
    web_manifest(tmp_path)
    page_only = smoke_plan()
    page_only['plan']['cases'][0]['journey'] = {'url': 'http://localhost', 'steps': [
        {'kind': 'assert', 'instruction': 'Page loaded', 'check': {'kind': 'expect_text', 'text': 'Ready'}},
    ]}
    report, _, _, agent = execute(tmp_path, [impact(ui=False), page_only, smoke_plan()], plan_only=True)
    assert report['status'] == 'planned'
    assert 'user action followed by a result assertion' in agent.prompts[2]


@pytest.mark.parametrize('argv', [
    ['python', '-'], ['uv', 'run', 'python3.14', '-c', 'import app'],
    ['node', '-e', 'require("app")'],
])
def test_inline_library_snippet_is_not_real_cli_action(argv):
    assert inline_harness(argv)
    assert not inline_harness(['uv', 'run', 'qa-app', 'create', 'dummy'])


def test_real_cli_actions_replace_imported_library_harness(tmp_path):
    cli_manifest(tmp_path)
    (tmp_path / 'app.py').write_text('''import json, pathlib, sys
path = pathlib.Path('dummy.json')
if sys.argv[1] == 'create':
    path.write_text(json.dumps({'prompt': sys.argv[2]}))
    print('Created: ' + sys.argv[2])
else:
    print('Stored: ' + json.loads(path.read_text())['prompt'])
''')
    plan = planned('terminal')
    test = BackendTest(case_id='backend', interface='terminal', steps=[
        {'kind': 'command', 'argv': [sys.executable, 'app.py', 'create', 'ov-dummy'],
         'expect': {'exit_code': 0, 'output': {'mode': 'equals', 'value': 'Created: ov-dummy\n'}}},
        {'kind': 'command', 'argv': [sys.executable, 'app.py', 'show'],
         'expect': {'exit_code': 0, 'output': {'mode': 'equals', 'value': 'Stored: ov-dummy\n'}}},
    ], checks={'Expected behavior': [0, 1]})
    bad = python_harness_suite('assert True', names=('Expected behavior',))
    agent = ScriptedAgent([plan, action('run_backend_test', **bad.model_dump()),
        action('run_backend_test', **test.model_dump())])
    artifacts = Artifacts(tmp_path / 'runs')
    runner = VerificationRunner(agent, LocalTools(tmp_path, artifacts, allow_exec=True),
        artifacts, progress=lambda _: None)
    report = asyncio.run(runner.run('Create and reopen a dummy record'))
    assert report['findings'][0]['status'] == 'passed'
    assert 'Application CLI smoke must invoke the real app command' in agent.prompts[2]
    commands = [e for e in artifacts.observations if e['tool'] == 'run_command']
    assert [e['arguments']['argv'][1:] for e in commands] == [
        ['app.py', 'create', 'ov-dummy'], ['app.py', 'show']]
    assert json.loads((tmp_path / 'dummy.json').read_text())['prompt'] == 'ov-dummy'


def test_backend_pr_runs_actual_ui_action_and_records_visual_evidence(tmp_path, web_app):
    web_manifest(tmp_path)
    target = case(web_app)
    target.coverage = 'regression'
    browser = smoke_plan()
    browser['plan']['cases'] = [target.model_dump()]
    assessment = impact(ui=False)
    agent = ScriptedAgent([assessment, planned('terminal'), browser,
        action('run_journey', case_id=target.id)])
    artifacts = Artifacts(tmp_path / 'runs')
    receipt = artifacts.record('read_file', {'path': 'fixture-routes.py'},
        {'text': 'The cart at /cart exposes Add item.'}, True)
    browser['plan']['cases'][0]['journey']['entry'] = {'evidence': [receipt['id']], 'controls': [
        {'kind': 'assert', 'instruction': 'Cart controls visible',
         'check': {'kind': 'expect_text', 'text': 'Add item'}}]}
    # ScriptedAgent holds the original dictionaries, so this updates its accepted plan.
    runner = VerificationRunner(agent, LocalTools(tmp_path, artifacts, headless=True), artifacts,
        change=Change(base='base', head='head', files=['app.html']), journey_executor=Actor(),
        progress=lambda _: None)
    report = asyncio.run(runner.run('Smoke app after backend change'))
    assert report['findings'][0]['status'] == 'passed'
    assert 'Manual QA requires a browser' in agent.prompts[2]
    trace = json.loads(next((artifacts.path / 'journeys').glob('*.json')).read_text())
    assert trace['steps'][0]['actions'] == 1
    assert trace['steps'][1]['status'] == 'passed'
    manifest = json.loads((artifacts.path / 'manifest.json').read_text())
    assert manifest['tests'][0]['interaction'] == 'user'
    assert any(a['type'] == 'screenshot' for a in manifest['artifacts'])
    assert runner.test_results[0].screenshots


def test_blocked_application_does_not_accept_backend_substitute(tmp_path):
    web_manifest(tmp_path)
    blocked = {'kind': 'finding', 'finding': {
        'case_id': 'greet', 'status': 'blocked', 'actual': 'Missing local startup configuration',
    }}
    report, browser, manifest, agent = execute(tmp_path, [
        impact(ui=False), smoke_plan(), planned('terminal'), blocked,
    ])
    assert report['findings'][0]['status'] == manifest['status'] == 'blocked'
    assert browser.calls == []
    assert 'plan is fixed' in agent.prompts[3]


def test_application_smoke_and_library_support_can_coexist(tmp_path):
    web_manifest(tmp_path)
    plan = smoke_plan()
    library = planned('terminal')['plan']['cases'][0]
    library.update(interaction='library', coverage='changed_behavior')
    plan['plan']['cases'].append(library)
    report, _, _, _ = execute(tmp_path, [impact(ui=False), plan], plan_only=True, max_cases=2)
    assert report['status'] == 'planned'
    assert [c['interaction'] for c in report['plan']['cases']] == ['user', 'library']



def test_known_application_cannot_pass_from_arbitrary_command_evidence(tmp_path):
    web_manifest(tmp_path)
    page = smoke_plan()
    page['plan']['cases'][0]['journey'] = case('http://localhost').journey.model_dump()
    page['plan']['cases'][0]['id'] = 'cart'
    fake = {'kind': 'finding', 'finding': {'case_id': 'cart', 'status': 'passed',
        'actual': 'A library check passed', 'evidence': ['E0001']}}
    blocked = {'kind': 'finding', 'finding': {'case_id': 'cart', 'status': 'blocked',
        'actual': 'Browser startup unavailable'}}
    agent = ScriptedAgent([page, action('run_command', argv=[sys.executable, '-c', 'print("ok")']),
        fake, blocked])
    artifacts = Artifacts(tmp_path / 'runs')
    runner = VerificationRunner(agent, LocalTools(tmp_path, artifacts, allow_exec=True),
        artifacts, progress=lambda _: None)
    report = asyncio.run(runner.run('Smoke app'))
    assert report['findings'][0]['status'] == 'blocked'
    assert 'latest generated test execution' in agent.prompts[3]


@pytest.mark.parametrize('surface', ['terminal', 'http'])
def test_help_and_health_alone_are_not_application_smoke(tmp_path, surface):
    cli_manifest(tmp_path)
    if surface == 'http':
        (tmp_path / 'pyproject.toml').write_text(
            '[project]\ndependencies=["fastapi"]\n[project.scripts]\nqa-app="app:main"\n')
    plan = planned(surface)
    steps = ([{'kind': 'command', 'argv': [sys.executable, 'app.py', '--help'],
               'expect': {'exit_code': 0}}] if surface == 'terminal' else
             [{'kind': 'http', 'url': 'http://localhost/api/health', 'expect': {'status': 200}}])
    test = BackendTest(case_id='backend', interface=surface, steps=steps,
        checks={'Expected behavior': [0]})
    blocked = {'kind': 'finding', 'finding': {'case_id': 'backend', 'status': 'blocked',
        'actual': 'Meaningful application action unavailable'}}
    agent = ScriptedAgent([plan, action('run_backend_test', **test.model_dump()), blocked])
    artifacts = Artifacts(tmp_path / 'runs')
    runner = VerificationRunner(agent, LocalTools(tmp_path, artifacts, allow_exec=True),
        artifacts, progress=lambda _: None)
    report = asyncio.run(runner.run('Smoke app'))
    assert report['findings'][0]['status'] == 'blocked'
    assert not any(e['tool'] in {'run_command', 'http_request'} for e in artifacts.observations)



def test_actor_cannot_pass_smoke_by_claiming_done_without_ui_action(tmp_path, web_app):
    web_manifest(tmp_path)
    target = case(web_app, text='Cart: 0')
    plan = smoke_plan()
    plan['plan']['cases'] = [target.model_dump()]
    artifacts = Artifacts(tmp_path / 'runs')
    receipt = artifacts.record('read_file', {'path': 'fixture-routes.py'},
        {'text': 'The cart at /cart exposes Add item.'}, True)
    plan['plan']['cases'][0]['journey']['entry'] = {'evidence': [receipt['id']], 'controls': [
        {'kind': 'assert', 'instruction': 'Cart controls visible',
         'check': {'kind': 'expect_text', 'text': 'Add item'}}]}
    agent = ScriptedAgent([plan, action('run_journey', case_id=target.id)])
    runner = VerificationRunner(agent, LocalTools(tmp_path, artifacts, headless=True), artifacts,
        journey_executor=Actor([complete()]), progress=lambda _: None)
    report = asyncio.run(runner.run('Smoke app'))
    assert report['findings'][0]['status'] == 'blocked'
    assert 'no successful UI action' in report['findings'][0]['actual']
    assert runner.test_results[0].checkpoints[-1].code == 'NO_USER_ACTION'
    exported = json.loads((artifacts.path / runner.test_results[0].test_file).with_suffix('.json').read_text())
    assert exported['steps'][-1]['kind'] == 'requires_verification'
