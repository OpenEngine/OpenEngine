"""Readable summaries describe execution without promoting plans to evidence."""

from open_verify.artifacts import Artifacts
from open_verify.reporting import observed_summary


def test_summary_prioritizes_failure_even_after_many_passed_checks():
    checkpoints = [dict(instruction=f"Check {i}", status="passed", detail="Matched") for i in range(8)]
    checkpoints.append(dict(instruction="Access after reload", status="failed", detail="Login prompt returned"))
    rows = observed_summary("", checkpoints, "failed")
    assert rows[0] == ("failed", "Access after reload: Login prompt returned")
    assert ("details", "6 more executed checks") in rows


def test_local_report_leads_with_captured_image_and_keeps_full_evidence(tmp_path):
    artifacts = Artifacts(tmp_path)
    (artifacts.path / 'journey.gif').write_bytes(b'GIF89afixture')
    receipt = artifacts.record('run_journey', {}, {
        'case_id': 'login', 'screenshots': ['journey.gif'],
        'checkpoints': [dict(instruction='Access survives reload', status='passed', detail='Visible')],
    }, True)
    explanation = ' '.join(['Lengthy explanation.'] * 80)
    artifacts.report({'request': 'Test login', 'status': 'complete', 'findings': [{
        'case_id': 'login', 'status': 'passed', 'actual': explanation,
        'evidence': [receipt['id']], 'reproduction': [],
    }]})
    visible, details = (artifacts.path / 'report.md').read_text().split('<details>', 1)
    assert visible.index('![Observed journey]') < visible.index('Access survives reload')
    assert 'Lengthy explanation' not in visible and explanation in details
    assert 'actions/E0001.json' in visible
    assert (artifacts.path / 'report.json').exists()


def test_harness_exception_is_visible_even_with_long_planned_check():
    rows = observed_summary('', [{
        'instruction': 'The scoper preserves supplied ticket metadata and source references while '
            'keeping every ticket proposed and respecting all supplied milestone requirements.',
        'status': 'blocked', 'code': 'HARNESS_ERROR',
        'detail': 'Python harness error: TypeError: Object of type TicketLayer is not JSON serializable',
    }], 'blocked')
    assert rows == [('blocked',
        'Python harness error: TypeError: Object of type TicketLayer is not JSON serializable')]


def test_health_diagnosis_precedes_locator_noise_without_erasing_failure():
    rows = observed_summary('', [
        {'instruction': 'Repository appears', 'status': 'failed', 'detail': 'Locator expected to be visible'},
        {'instruction': 'Reload persists', 'status': 'blocked', 'code': 'NOT_RUN', 'detail': 'Not run'},
        {'instruction': 'Host: application health after user actions', 'status': 'blocked',
         'code': 'FIXTURE_ERROR', 'detail': 'QA fixture error: no scripted scenario matches implementation prompt'},
    ], 'blocked')
    assert rows[0] == ('blocked', 'QA fixture error: no scripted scenario matches implementation prompt')
    assert rows[1][0] == 'failed'
    assert ('not run', '1 remaining checks') in rows


def test_regression_and_scripted_provider_limits_are_visible(tmp_path):
    artifacts = Artifacts(tmp_path)
    case = {'id': 'smoke', 'title': 'Search', 'expected': 'Matches shown',
        'coverage': 'regression', 'journey': {'scripted_providers': True}}
    state = {'request': 'Verify PR', 'status': 'complete',
        'impact': {'decision': 'verify', 'reason': 'Backend contracts changed'},
        'plan': {'project_summary': 'Search app', 'cases': [case], 'questions': [], 'assumptions': []},
        'findings': [{'case_id': 'smoke', 'status': 'passed', 'actual': 'Matches shown',
            'evidence': [], 'reproduction': []}]}
    artifacts.report(state)
    visible = (artifacts.path / 'report.md').read_text().split('<details>')[0]
    assert '**Scope:** changed behavior unverified.' in visible
    assert 'scripted providers; live integrations unverified' in visible
    case['coverage'] = 'changed_behavior'
    case['journey']['scripted_providers'] = False
    artifacts.report(state)
    visible = (artifacts.path / 'report.md').read_text().split('<details>')[0]
    assert '**Scope:**' not in visible and '**Substitutions:**' not in visible


def test_caption_uses_final_product_evidence_without_repeating_model_prose():
    from open_verify.reporting import evidence_caption
    rows = evidence_caption('An unnecessarily long explanation. ' * 100, [
        {'instruction': 'The form is visible', 'status': 'passed'},
        {'instruction': 'Reload preserves the submitted message and response', 'status': 'passed'},
        {'instruction': 'Host: application health after user actions', 'status': 'passed'},
    ], 'passed')
    assert rows == [('passed', '3 checks. Reload preserves the submitted message and response')]
    assert len(rows[0][1].split()) <= 20


def test_caption_keeps_failed_check_even_when_health_passes():
    from open_verify.reporting import evidence_caption
    rows = evidence_caption('Healthy', [
        {'instruction': 'Saved item appears', 'status': 'failed', 'detail': 'Item missing'},
        {'instruction': 'Host: application health', 'status': 'passed'},
        {'instruction': 'Reload', 'status': 'blocked', 'code': 'NOT_RUN'},
    ], 'failed')
    assert rows[0][0] == 'failed' and 'Item missing' in rows[0][1]
    assert rows[-1] == ('not run', '1 check')


def test_blocked_caption_preserves_executed_product_examples():
    from open_verify.reporting import evidence_caption
    points = [{'instruction': text, 'status': 'passed'} for text in [
        'The form is visible', 'The submitted message is visible',
        'The completed response is visible', 'The conversation reaches Idle',
        'Host: application health after user actions']]
    points += [{'instruction': 'Reload retains the message', 'status': 'blocked', 'code': 'NOT_RUN'}]
    rows = evidence_caption('Step exhausted its model-call budget', points, 'blocked')
    assert rows[0] == ('blocked', 'Step exhausted its model-call budget')
    assert rows[1] == ('passed', 'The submitted message is visible → The completed response is visible → The conversation reaches Idle')
    assert rows[-1] == ('not run', '1 check')
    assert 'Host:' not in rows[1][1] and 'Reload' not in rows[1][1]
