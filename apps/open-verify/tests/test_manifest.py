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
    assert [item.path for item in manifest.artifacts] == ["test.py", "valid.mp4"]
    assert len(manifest.omissions) == 3
    assert Manifest.model_validate(json.loads((bundle / "manifest.json").read_text())) == manifest
    for item in manifest.artifacts:
        assert item.size_bytes == (bundle / item.path).stat().st_size


def test_cleanup_failure_prevents_success_and_non_ui_change_suppresses_media(tmp_path):
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
    assert [item.type for item in manifest.artifacts] == ["test"]
