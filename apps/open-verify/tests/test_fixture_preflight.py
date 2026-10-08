"""Declared scripted providers must pass fixed contract probes before UI actions."""

import asyncio
import json
import sys

import pytest
from pydantic import ValidationError
from test_journeys import Actor, case
from test_tools import web_app as web_app
from test_workflow import ScriptedAgent, action

from open_verify.artifacts import Artifacts
from open_verify.journey import JourneyRunner
from open_verify.models import Case
from open_verify.runner import VerificationRunner
from open_verify.tools import LocalTools

# A minimal scenario matcher reproducing the real bad-fixture failure from the run.
PROBE = '''import json
from pathlib import Path
script = json.loads(Path('provider.json').read_text())
prompts = json.loads(Path('prompts.json').read_text())
for name, prompt in prompts.items():
    matches = [s for s in script['scenarios'] if s['when'] in prompt]
    assert matches, f'no scripted scenario matches {name} prompt'
    assert matches[0]['terminal'] == 'complete_step', f'{name} lacks terminal response'
print('All actual workflow prompts select valid terminal responses')
'''


def target(url):
    data = case(url).model_dump()
    data['journey'].update(scripted_providers=True, setup_probes=[{
        'instruction': 'Actual workflow prompts select valid terminal responses',
        'argv': [sys.executable, '-'], 'stdin': PROBE,
    }])
    return Case.model_validate(data)


def files(root, *, valid):
    (root / 'prompts.json').write_text(json.dumps({'implementation': 'Implement the change. Task: QA'}))
    (root / 'provider.json').write_text(json.dumps({'scenarios': [{
        'when': 'Implement the change.' if valid else 'Implement the requested change in the provided workspace',
        'terminal': 'complete_step',
    }]}))


def test_scripted_provider_declaration_requires_a_probe():
    data = case('http://localhost').model_dump()
    data['journey']['scripted_providers'] = True
    with pytest.raises(ValidationError, match='Scripted providers require setup_probes'):
        Case.model_validate(data)


def test_bad_scenario_match_blocks_before_any_browser_action(tmp_path):
    files(tmp_path, valid=False)
    artifacts = Artifacts(tmp_path / 'runs')
    selected = target('http://localhost')
    plan = {'kind': 'plan', 'plan': {'project_summary': 'Fixture QA', 'startup': [],
        'cases': [selected.model_dump()]}}
    actor = Actor()
    runner = VerificationRunner(ScriptedAgent([plan, *[action('run_journey', case_id=selected.id)] * 3]),
        LocalTools(tmp_path, artifacts, allow_exec=True), artifacts,
        journey_executor=actor, progress=lambda _: None)
    report = asyncio.run(runner.run('Smoke the app'))
    assert report['findings'][0]['status'] == 'blocked'
    visible = (artifacts.path / 'report.md').read_text().split('<details>', 1)[0]
    assert 'no scripted scenario matches implementation prompt' in visible
    readiness = [e for e in artifacts.observations if e['tool'] == 'setup_readiness']
    assert len(readiness) == 3
    assert all((artifacts.path / e['artifact']).is_file() for e in readiness)
    assert all(e['result']['code'] == 'FIXTURE_SETUP_ERROR' for e in readiness)
    assert not any(e['tool'].startswith('browser_') for e in artifacts.observations)
    assert not actor.contexts and not runner.test_results
    commands = [e for e in artifacts.observations if e['tool'] == 'run_command']
    assert len(commands) == 3
    assert all('no scripted scenario matches implementation prompt' in e['result']['output'] for e in commands)
    assert all(e['arguments']['stdin'] == PROBE for e in commands)


def test_probe_repairs_fixture_without_changing_check_and_then_allows_readiness(tmp_path, web_app):
    files(tmp_path, valid=False)
    artifacts = Artifacts(tmp_path / 'runs')
    engine = LocalTools(tmp_path, artifacts, allow_exec=True, headless=True)
    selected = target(web_app)
    before = selected.model_dump()
    runner = JourneyRunner(engine, artifacts, Actor(), progress=lambda _: None)

    async def run():
        try:
            first = await runner.check_readiness(selected)
            assert first['code'] == 'FIXTURE_SETUP_ERROR'
            assert not any(e['tool'].startswith('browser_') for e in artifacts.observations)
            files(tmp_path, valid=True)
            second = await runner.check_readiness(selected)
            assert second['status'] == 'passed'
            assert any(e['tool'] == 'browser_open' for e in artifacts.observations)
        finally:
            await engine.close()

    asyncio.run(run())
    assert selected.model_dump() == before


def test_probe_cannot_bypass_allow_exec(tmp_path):
    artifacts = Artifacts(tmp_path / 'runs')
    engine = LocalTools(tmp_path, artifacts, allow_exec=False)
    result = asyncio.run(JourneyRunner(engine, artifacts, Actor(),
        progress=lambda _: None).check_readiness(target('http://localhost')))
    assert result['status'] == 'blocked'
    assert not any(e['tool'].startswith('browser_') for e in artifacts.observations)
