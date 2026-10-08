import json

from open_verify.manifest import Manifest, write_manifest
from open_verify.media import MAX_VIDEO_BYTES
from open_verify.test_spec import TestResult as RunnerResult


def test_manifest_limits_media_and_rejects_escaping_paths(tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (tmp_path / "outside.png").write_bytes(b"outside")
    (bundle / "test.py").write_text("pass", encoding="utf-8")
    (bundle / "valid.mp4").write_bytes(b"video")
    with (bundle / "oversized.mp4").open("wb") as file:
        file.truncate(MAX_VIDEO_BYTES)
    result = RunnerResult(
        case_id="a",
        status="passed",
        detail="done",
        test_file="test.py",
        rerun=["python", "test.py"],
        screenshots=["../outside.png"],
        videos=["valid.mp4", "oversized.mp4", "missing.mp4"],
    )
    report = {
        "status": "complete",
        "findings": [{"case_id": "a", "status": "passed", "actual": "done"}],
        "impact": {
            "decision": "verify",
            "reason": "UI change",
            "material_ui_change": True,
            "journeys": ["Example"],
        },
    }
    manifest = write_manifest(bundle, report, None, [result])
    assert [item.path for item in manifest.artifacts] == ["test.py"]
    assert len(manifest.omissions) == 3
    assert Manifest.model_validate(json.loads((bundle / "manifest.json").read_text())) == manifest
    for item in manifest.artifacts:
        assert item.size_bytes == (bundle / item.path).stat().st_size


def test_cleanup_failure_prevents_success_and_backend_smoke_keeps_media(tmp_path):
    (tmp_path / "test.py").write_text("pass", encoding="utf-8")
    (tmp_path / "image.png").write_bytes(b"image")
    result = RunnerResult(
        case_id="a",
        status="passed",
        detail="done",
        test_file="test.py",
        rerun=["python", "test.py"],
        screenshots=["image.png"],
    )
    manifest = write_manifest(
        tmp_path,
        {"status": "complete", "findings": []},
        None,
        [result],
        cleanup_errors=["browser did not close"],
    )
    assert manifest.status == "blocked"
    assert [item.type for item in manifest.artifacts] == ["test", "screenshot"]


def test_only_final_journey_summary_and_test_are_publishable(tmp_path):
    results = []
    for attempt in (1, 2):
        names = [f'test-{attempt}.py', f'initial-{attempt}.png', f'final-{attempt}.png', f'journey-{attempt}.gif']
        for name in names:
            (tmp_path / name).write_bytes(b'fixture')
        results.append(RunnerResult(case_id='cart', status='failed' if attempt == 1 else 'passed',
            detail='Cart lifecycle', test_file=names[0], rerun=['python', names[0]], screenshots=names[1:]))
    report = {'status': 'complete', 'findings': [{'case_id': 'cart', 'status': 'passed', 'actual': 'done'}],
              'impact': {'decision': 'verify', 'reason': 'UI', 'material_ui_change': True, 'journeys': ['cart']}}
    manifest = write_manifest(tmp_path, report, None, results)
    assert [item.path for item in manifest.artifacts] == ['test-2.py', 'journey-2.gif']
    assert len(manifest.tests) == 1 and manifest.tests[0].status == 'passed'
    assert (tmp_path / 'initial-1.png').exists() and (tmp_path / 'test-1.py').exists()
    # Missing final animation must not substitute evidence from the older attempt.
    results[-1].screenshots = ['initial-2.png', 'final-2.png']
    manifest = write_manifest(tmp_path, report, None, results)
    assert [item.path for item in manifest.artifacts] == ['test-2.py', 'final-2.png']
    assert any('GIF unavailable' in reason for reason in manifest.omissions)


def test_manifest_carries_scripted_provider_disclosure_from_journey(tmp_path):
    (tmp_path / 'test.py').write_text('pass')
    manifest = write_manifest(tmp_path, {
        'status': 'complete', 'findings': [],
        'plan': {'cases': [{'id': 'smoke', 'journey': {'scripted_providers': True}}]},
    }, None, [RunnerResult(case_id='smoke', status='passed', detail='Reload retained message',
        test_file='test.py', rerun=['python', 'test.py'])])
    assert manifest.tests[0].scripted_providers is True
    saved = Manifest.model_validate_json((tmp_path / 'manifest.json').read_text())
    assert saved.tests[0].scripted_providers is True
