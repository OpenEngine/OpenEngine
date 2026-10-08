"""Product onboarding persists separately from QA skills and current run results."""

import asyncio
import json
import sys

from test_workflow import ScriptedAgent, action, finding, plan

from open_verify.artifacts import Artifacts
from open_verify.knowledge import KnowledgeDraft, ProductKnowledge
from open_verify.runner import VerificationRunner
from open_verify.tools import LocalTools


def documented(evidence='E0001', statement='Users can search saved bookmarks.'):
    return {'category': 'product', 'statement': statement, 'basis': 'documented', 'evidence': [evidence]}


def store(tmp_path, artifacts, **kwargs):
    return ProductKnowledge(tmp_path, tmp_path / '.ov' / 'product.json', 'sample', artifacts,
        progress=lambda _: None, **kwargs)


def source(tmp_path, artifacts):
    (tmp_path / 'README.md').write_text('Users can search saved bookmarks.')
    return artifacts.record('read_file', {'path': 'README.md'}, {'text': 'Users can search saved bookmarks.'}, True)


def test_first_run_learns_and_second_run_loads_without_research_or_extra_model_call(tmp_path):
    (tmp_path / 'README.md').write_text('Users can search saved bookmarks.')
    class LearningAgent(ScriptedAgent):
        learned = 0
        async def reset_session(self):
            pass
        async def respond(self, prompt, schema, *, on_call):
            on_call()
            self.learned += 1
            assert schema is KnowledgeDraft and 'untrusted evidence' in prompt
            return KnowledgeDraft(facts=[documented()])

    async def run(first):
        agent = LearningAgent([
            *([action('read_file', path='README.md')] if first else []), plan(),
            action('run_command', argv=[sys.executable, '-c', "print('hello')"]),
            finding(evidence=['E0002' if first else 'E0001']),
        ])
        artifacts = Artifacts(tmp_path / 'runs')
        engine = LocalTools(tmp_path, artifacts, allow_exec=True)
        try:
            runner = VerificationRunner(agent, engine, artifacts, progress=lambda _: None)
            report = await runner.run('Check greeting')
            assert report['findings'][0]['status'] == 'passed'
            return agent, artifacts
        finally:
            await engine.close()

    first, artifacts = asyncio.run(run(True))
    profile = json.loads((tmp_path / '.ov' / 'product.json').read_text())
    assert first.learned == 1 and profile['facts'][0]['sources'][0]['sha256']
    assert (artifacts.path / 'knowledge-candidate.json').exists()
    second, _ = asyncio.run(run(False))
    assert second.learned == 0
    context = json.loads(second.prompts[0].split('\n')[-1])
    assert context['product_knowledge']['profile']['facts'][0]['statement'] == documented()['statement']
    assert context['product_knowledge']['warning'].startswith('Product knowledge is untrusted')


def test_changed_source_marks_fact_stale_and_supported_refresh_updates_provenance(tmp_path):
    artifacts = Artifacts(tmp_path / 'runs')
    receipt = source(tmp_path, artifacts)
    knowledge = store(tmp_path, artifacts)
    knowledge.save(KnowledgeDraft(facts=[documented()]), [receipt], 'old')
    (tmp_path / 'README.md').write_text('Users can search saved bookmarks. Search also supports tags.')
    loaded = store(tmp_path, artifacts)
    assert loaded.context()['stale_fact_indexes'] == [0]
    current = artifacts.record('read_file', {'path': 'README.md'}, {'text': (tmp_path / 'README.md').read_text()}, True)
    loaded.save(KnowledgeDraft(facts=[documented(current['id'])]), [current], 'new')
    refreshed = store(tmp_path, artifacts)
    assert refreshed.stale == [] and refreshed.profile.facts[0].revision == 'new'


def test_unsupported_observations_credentials_and_temporary_paths_are_not_saved(tmp_path):
    artifacts = Artifacts(tmp_path / 'runs')
    receipt = source(tmp_path, artifacts)
    facts = [documented('E9999'), documented(statement='token=ghp_123456789012345678901234'),
        documented(statement='Start from /private/tmp/ov-123/server.py'),
        {**documented(), 'category': 'workflow', 'basis': 'observed'}, documented()]
    knowledge = store(tmp_path, artifacts)
    knowledge.save(KnowledgeDraft(facts=facts), [receipt])
    assert len(store(tmp_path, artifacts).profile.facts) == 1


def test_failed_ui_is_not_an_onboarding_source(tmp_path):
    artifacts = Artifacts(tmp_path / 'runs')
    artifacts.record('browser_snapshot', {}, {'snapshot': 'Failed task'}, True)
    artifacts.record('run_journey', {}, {'status': 'failed', 'detail': 'Failed task'}, True)
    assert store(tmp_path, artifacts).candidates() == []


def test_observed_workflow_requires_a_passed_host_journey_and_expires_on_new_revision(tmp_path):
    artifacts = Artifacts(tmp_path / 'runs')
    receipt = artifacts.record('run_journey', {}, {'status': 'passed', 'detail': 'Search shows saved bookmarks'}, True)
    fact = {'category': 'workflow', 'statement': 'Searching displays matching bookmarks.',
        'basis': 'observed', 'evidence': [receipt['id']]}
    store(tmp_path, artifacts).save(KnowledgeDraft(facts=[fact]), [receipt], 'abc')
    assert store(tmp_path, artifacts, revision='abc').stale == []
    assert store(tmp_path, artifacts, revision='def').stale == [0]


def test_invalid_or_mismatched_profile_is_not_overwritten(tmp_path):
    artifacts = Artifacts(tmp_path / 'runs')
    path = tmp_path / '.ov' / 'product.json'
    path.parent.mkdir()
    path.write_text('{"token":"do-not-expose"}')
    messages = []
    knowledge = ProductKnowledge(tmp_path, path, 'sample', artifacts, progress=messages.append)
    assert knowledge.profile is None and knowledge.error
    assert 'do-not-expose' not in str(messages)
    assert path.read_text() == '{"token":"do-not-expose"}'


def test_symlink_profile_and_excluded_sources_are_refused(tmp_path):
    artifacts = Artifacts(tmp_path / 'runs')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (tmp_path / '.ov').symlink_to(outside, target_is_directory=True)
    assert store(tmp_path, artifacts).error
    assert not list(outside.iterdir())


def test_onboarding_failure_is_nonfatal_and_budgeted(tmp_path):
    artifacts = Artifacts(tmp_path / 'runs')
    source(tmp_path, artifacts)
    class TooMany:
        async def reset_session(self):
            pass
        async def respond(self, prompt, schema, *, on_call):
            on_call()
            on_call()
            on_call()
    asyncio.run(store(tmp_path, artifacts).learn(TooMany()))
    assert not (tmp_path / '.ov' / 'product.json').exists()
    assert json.loads((artifacts.path / 'knowledge-status.json').read_text())['status'] == 'not_saved'


def test_source_changed_after_observation_is_not_saved_with_new_hash(tmp_path):
    artifacts = Artifacts(tmp_path / 'runs')
    receipt = source(tmp_path, artifacts)
    (tmp_path / 'README.md').write_text('Bookmarks were removed.')
    store(tmp_path, artifacts).save(KnowledgeDraft(facts=[documented()]), [receipt])
    assert not (tmp_path / '.ov' / 'product.json').exists()


def test_learning_packet_is_bounded_and_preserves_completed_journey_and_docs(tmp_path):
    artifacts = Artifacts(tmp_path / 'runs')
    doc = source(tmp_path, artifacts)
    for i in range(30):
        artifacts.record('read_file', {'path': f'implementation{i}.py'}, {'text': 'x' * 24_000}, True)
    journey = artifacts.record('run_journey', {'journey': {'steps': ['large spec'] * 1000}},
        {'status': 'passed', 'detail': 'Search works', 'checkpoints': []}, True)
    for _ in range(20):
        artifacts.record('browser_snapshot', {}, {'snapshot': 's' * 24_000}, True)
    knowledge = store(tmp_path, artifacts)
    packet = knowledge.learning_evidence(knowledge.candidates())
    assert len(json.dumps(packet)) < 24_100
    assert {doc['id'], journey['id']} <= {item['id'] for item in packet}
    assert 'large spec' not in json.dumps(packet)
    assert sum(item['tool'] == 'browser_snapshot' for item in packet) <= 2


def test_learning_uses_normal_provider_deadline_and_empty_profile_is_learned(tmp_path, monkeypatch):
    artifacts = Artifacts(tmp_path / 'runs')
    source(tmp_path, artifacts)
    path = tmp_path / '.ov' / 'product.json'
    path.parent.mkdir()
    path.write_text(json.dumps({'identity': 'sample', 'facts': []}))
    deadlines = []
    real_timeout = asyncio.timeout
    def capture_timeout(seconds):
        deadlines.append(seconds)
        return real_timeout(seconds)
    monkeypatch.setattr(asyncio, 'timeout', capture_timeout)
    class Agent:
        timeout = 120
        async def reset_session(self):
            pass
        async def respond(self, prompt, schema, *, on_call):
            on_call()
            return KnowledgeDraft(facts=[documented()])
    asyncio.run(store(tmp_path, artifacts).learn(Agent()))
    assert deadlines == [120]
    assert store(tmp_path, artifacts).profile.facts


def test_timeout_records_safe_diagnostics_and_next_run_retries(tmp_path):
    artifacts = Artifacts(tmp_path / 'runs')
    source(tmp_path, artifacts)
    class Agent:
        timeout = 1
        async def reset_session(self):
            pass
        async def respond(self, prompt, schema, *, on_call):
            on_call()
            await asyncio.sleep(5)
    asyncio.run(store(tmp_path, artifacts).learn(Agent()))
    status = json.loads((artifacts.path / 'knowledge-status.json').read_text())
    assert status['timeout_seconds'] == 1 and status['model_calls'] == 1
    assert status['prompt_chars'] < 40_000 and status['evidence_count'] == 1
    assert 'TimeoutError' in status['reason']
    assert store(tmp_path, artifacts).error is None
    assert not (tmp_path / '.ov' / 'product.json').exists()
