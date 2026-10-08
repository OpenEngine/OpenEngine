"""Repository exploration must leave room for actual QA execution."""

import asyncio
import json
import sys

import pytest
from test_workflow import ScriptedAgent, action, finding, plan

from open_verify.artifacts import Artifacts
from open_verify.runner import VerificationRunner
from open_verify.tools import LocalTools


def test_discovery_cap_preserves_room_for_plan_and_real_command(tmp_path):
    for index in range(5):
        (tmp_path / f'source{index}.txt').write_text(f'Content {index}')
    agent = ScriptedAgent([
        *[action('read_file', path=f'source{i}.txt') for i in range(5)],
        plan(), action('run_command', argv=[sys.executable, '-c', "print('hello')"]),
        finding(evidence=['E0006']),
    ])
    artifacts = Artifacts(tmp_path / 'runs')
    engine = LocalTools(tmp_path, artifacts, allow_exec=True)
    runner = VerificationRunner(agent, engine, artifacts, max_steps=12, progress=lambda _: None)
    report = asyncio.run(runner.run('Verify greeting'))
    assert report['status'] == 'complete'
    assert report['findings'][0]['status'] == 'passed'
    assert artifacts.observations[4]['result']['code'] == 'INSPECTION_BUDGET'
    after_cap = json.loads(agent.prompts[4].split('\n')[-1])
    assert not after_cap['inspection_budget']['inspection_available']
    assert 'read_file' not in after_cap['tools']
    assert len(after_cap['inspection_budget']['inspected_sources']) == 4
    assert report['steps'] == 8
    assert 'hello' in artifacts.observations[5]['result']['output']


def test_duplicate_prefix_and_missing_file_reuse_prior_evidence_without_io(tmp_path):
    (tmp_path / 'source.txt').write_text('α' * 25000)
    artifacts = Artifacts(tmp_path / 'runs')
    engine = LocalTools(tmp_path, artifacts)
    original = engine.execute
    reads = []

    async def execute(name, args, **kwargs):
        reads.append((name, args))
        return await original(name, args, **kwargs)
    engine.execute = execute
    runner = VerificationRunner(None, engine, artifacts, progress=lambda _: None)

    async def apply(**arguments):
        return (await runner.apply({'stage': 'discover', 'steps': 1,
            'decision': action('read_file', **arguments)}))['observation']

    async def run():
        first = await apply(path='source.txt')
        again = await apply(path='./source.txt', offset=0, limit=24000)
        assert again['result']['code'] == 'ALREADY_INSPECTED'
        assert again['result']['evidence'] == first['id']
        assert again['result']['previous_result']['next_offset'] == 24000
        tail = await apply(path='source.txt', offset=24000, limit=1000)
        assert tail['result']['text'] == 'α' * 1000 and not tail['result']['truncated']
        missing = await apply(path='OV.md')
        repeated = await apply(path='OV.md')
        assert not missing['ok'] and repeated['result']['evidence'] == missing['id']
        assert len(reads) == 3
    asyncio.run(run())


def test_setup_and_explicit_refresh_invalidate_stale_source_evidence(tmp_path):
    (tmp_path / 'source.txt').write_text('old')
    artifacts = Artifacts(tmp_path / 'runs')
    engine = LocalTools(tmp_path, artifacts, allow_exec=True)
    runner = VerificationRunner(None, engine, artifacts, progress=lambda _: None)

    async def apply(tool, **arguments):
        return (await runner.apply({'stage': 'execute', 'steps': 1,
            'decision': action(tool, **arguments)}))['observation']

    async def run():
        assert (await apply('read_file', path='source.txt'))['result']['text'] == 'old'
        await apply('run_command', argv=[sys.executable, '-c',
            "from pathlib import Path; Path('source.txt').write_text('new')"])
        assert (await apply('read_file', path='source.txt'))['result']['text'] == 'new'
        (tmp_path / 'source.txt').write_text('external')
        assert (await apply('read_file', path='source.txt', refresh=True))['result']['text'] == 'external'
    asyncio.run(run())


def test_execution_reserve_rejects_reads_but_allows_actual_operations(tmp_path):
    artifacts = Artifacts(tmp_path / 'runs')
    runner = VerificationRunner(None, LocalTools(tmp_path, artifacts, allow_exec=True), artifacts,
        max_steps=12, progress=lambda _: None)

    async def run():
        state = {'stage': 'execute', 'steps': 11, 'decision': action('read_file', path='missing')}
        read = await runner.apply(state)
        assert read['observation']['result']['code'] == 'INSPECTION_BUDGET'
        state['decision'] = action('run_command', argv=[sys.executable, '-c', "print('ran')"])
        executed = await runner.apply(state)
        assert executed['observation']['result']['exit_code'] == 0
    asyncio.run(run())


@pytest.mark.parametrize('arguments', [
    {'offset': -1}, {'offset': True}, {'offset': 10_000_001}, {'limit': 24001}, {'limit': 0},
    {'path': '../secret'}, {'path': '.env'},
])
def test_file_sections_keep_bounds_and_secret_path_guards(tmp_path, arguments):
    engine = LocalTools(tmp_path, Artifacts(tmp_path / 'runs'))
    receipt = asyncio.run(engine.execute('read_file', arguments))
    assert not receipt['ok']


def test_malformed_inspection_is_refused_by_engine_not_a_runner_crash(tmp_path):
    artifacts = Artifacts(tmp_path / 'runs')
    runner = VerificationRunner(None, LocalTools(tmp_path, artifacts), artifacts, progress=lambda _: None)
    response = asyncio.run(runner.apply({'stage': 'discover', 'steps': 1,
        'decision': action('read_file', path={'invalid': True})}))
    assert not response['observation']['ok']


def test_source_index_survives_case_context_reset(tmp_path):
    (tmp_path / 'README.md').write_text('Useful source')
    agent = ScriptedAgent([action('read_file', path='README.md'), plan(),
        action('run_command', argv=[sys.executable, '-c', "print('hello')"]),
        finding(evidence=['E0002'])])
    artifacts = Artifacts(tmp_path / 'runs')
    runner = VerificationRunner(agent, LocalTools(tmp_path, artifacts, allow_exec=True), artifacts,
        progress=lambda _: None)
    report = asyncio.run(runner.run('Check greeting'))
    assert report['status'] == 'complete'
    context = json.loads(agent.prompts[2].split('\n')[-1])
    source = context['inspection_budget']['inspected_sources'][0]
    assert source['evidence'] == 'E0001' and source['arguments']['path'] == 'README.md'


def test_real_cli_prints_incomplete_reason_before_artifact_paths(tmp_path, capsys):
    from pathlib import Path

    from open_verify.cli import main

    (tmp_path / '.git').mkdir()
    script = tmp_path / 'decisions.json'
    script.write_text(json.dumps([plan()]))
    code = main(['Check greeting', '--project', str(tmp_path), '--output', str(tmp_path / 'runs'),
        '--agent', 'fixture', '--agent-command', json.dumps([
            sys.executable, str(Path(__file__).with_name('fake_agent.py')), str(script)]),
        '--max-steps', '1', '--cache', 'off', '--headless'])
    assert code == 2
    output = capsys.readouterr().out
    assert 'Run incomplete: Action/decision budget exhausted before any case result was recorded.' in output
    assert output.index('Run incomplete:') < output.index('Report:') < output.index('Manifest:')
    manifest = json.loads(next((tmp_path / 'runs').glob('*/manifest.json')).read_text())
    assert manifest['status'] == 'incomplete' and not manifest['tests']
