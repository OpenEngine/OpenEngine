import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from engine.runtime.verification import load_verification, publish_verification

HEAD = "a" * 40
PR = "https://github.com/owner/repo/pull/42"


def bundle(root, status="passed"):
    root.mkdir(exist_ok=True)
    files = [
        ("test.py", "test", "text/x-python", b"async def test_change(page): pass\n"),
        ("view.png", "screenshot", "image/png", b"\x89PNG\r\n\x1a\nfixture"),
        ("view.mp4", "video", "video/mp4", b"\x00\x00\x00\x18ftypfixture"),
    ]
    for name, _, _, content in files:
        (root / name).write_bytes(content)
    data = {
        "schema_version": 1,
        "status": status,
        "change": {"head": HEAD, "base": "b" * 40, "include_working_tree": False},
        "tests": [{"case_id": "C1", "status": "passed", "path": "test.py"}],
        "artifacts": [
            {
                "path": name,
                "type": kind,
                "media_type": mime,
                "size_bytes": len(content),
                "case_id": "C1",
            }
            for name, kind, mime, content in files
        ],
        "omissions": [],
    }
    if status == "skipped":
        data.update(tests=[], artifacts=[])
    (root / "manifest.json").write_text(json.dumps(data))
    return root / "manifest.json"


def collaborators():
    request = SimpleNamespace(url=PR, head_sha=HEAD, comments=())
    source = SimpleNamespace(
        view_change_request=AsyncMock(return_value=request),
        add_comment=AsyncMock(return_value=SimpleNamespace(url=PR + "#issuecomment-1")),
    )
    uploader = SimpleNamespace(
        upload=AsyncMock(
            side_effect=lambda pr, head, a: "https://github.com/assets/" + a.name
        )
    )
    return source, uploader


def publish(path, source, uploader):
    return asyncio.run(
        publish_verification(
            path,
            workspace_id="ws-test",
            pr_url=PR,
            source_control=source,
            uploader=uploader,
        )
    )


def test_publishes_only_manifest_evidence_and_reuses_comment(tmp_path):
    path = bundle(tmp_path)
    (tmp_path / ".env").write_text("secret")
    source, uploader = collaborators()
    result = publish(path, source, uploader)
    assert result["status"] == "passed"
    assert uploader.upload.await_count == 3
    body = source.add_comment.call_args.args[1]
    assert "![screenshot" in body and "[video" in body and "[test" in body
    assert "secret" not in body
    source.view_change_request.return_value.comments = [
        SimpleNamespace(body=body, url=result["comment_url"])
    ]
    assert publish(path, source, uploader)["comment_url"] == result["comment_url"]
    assert source.add_comment.await_count == 1


@pytest.mark.parametrize(
    "mutation",
    ["revision", "dirty", "traversal", "size", "version", "symlink", "oversize"],
)
def test_invalid_bundle_never_uploads_or_comments(tmp_path, mutation):
    path = bundle(tmp_path)
    data = json.loads(path.read_text())
    if mutation == "revision":
        data["change"]["head"] = "c" * 40
    if mutation == "dirty":
        data["change"]["include_working_tree"] = True
    if mutation == "traversal":
        data["artifacts"][0]["path"] = "../secret.py"
    if mutation == "size":
        data["artifacts"][0]["size_bytes"] += 1
    if mutation == "version":
        data["schema_version"] = 99
    if mutation == "symlink":
        (tmp_path / "test.py").unlink()
        (tmp_path / "test.py").symlink_to(tmp_path / "view.png")
    if mutation == "oversize":
        with (tmp_path / "view.mp4").open("wb") as f:
            f.truncate(10_000_000)
        data["artifacts"][2]["size_bytes"] = 10_000_000
    path.write_text(json.dumps(data))
    source, uploader = collaborators()
    with pytest.raises(ValueError):
        publish(path, source, uploader)
    uploader.upload.assert_not_called()
    source.add_comment.assert_not_called()


def test_skip_has_no_publication(tmp_path):
    source, uploader = collaborators()
    assert publish(bundle(tmp_path, "skipped"), source, uploader)["status"] == "skipped"
    uploader.upload.assert_not_called()
    source.add_comment.assert_not_called()


def test_head_change_during_upload_does_not_post_stale_comment(tmp_path):
    source, uploader = collaborators()
    source.view_change_request.side_effect = [
        SimpleNamespace(url=PR, head_sha=HEAD),
        SimpleNamespace(url=PR, head_sha="c" * 40),
    ]
    with pytest.raises(ValueError, match="changed during"):
        publish(bundle(tmp_path), source, uploader)
    source.add_comment.assert_not_called()


def test_failed_upload_does_not_post_success(tmp_path):
    source, uploader = collaborators()
    uploader.upload.side_effect = RuntimeError("upload failed")
    with pytest.raises(RuntimeError):
        publish(bundle(tmp_path), source, uploader)
    source.add_comment.assert_not_called()


def test_failed_verification_keeps_failure_evidence(tmp_path):
    source, uploader = collaborators()
    assert publish(bundle(tmp_path, "failed"), source, uploader)["status"] == "failed"
    assert "**failed**" in source.add_comment.call_args.args[1]
    assert (
        len(load_verification(tmp_path / "manifest.json", expected_head=HEAD).artifacts)
        == 3
    )
