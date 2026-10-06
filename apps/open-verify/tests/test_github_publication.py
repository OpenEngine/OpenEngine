import asyncio
import json

import pytest

from open_verify.changes import Change
from open_verify.github import GitHub, PullRequest
from open_verify.manifest import write_manifest
from open_verify.test_spec import TestResult as Result

HEAD, BASE = "a" * 40, "b" * 40
TARGET = PullRequest("owner/repo", 1, HEAD, BASE, BASE)


def bundle(root, status="complete"):
    (root / "test.py").write_text("async def test_change(page): pass")
    (root / "screen.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    (root / "video.mp4").write_bytes(b"\x00\x00\x00\x18ftypfixture")
    write_manifest(
        root,
        {
            "status": status,
            "findings": [{"case_id": "C1", "status": "passed", "actual": "visible"}],
            "impact": {
                "decision": "verify",
                "reason": "UI changed",
                "material_ui_change": True,
                "journeys": ["login"],
            },
        },
        Change(base=BASE, head=HEAD),
        [
            Result(
                case_id="C1",
                status="passed",
                detail="passed",
                test_file="test.py",
                rerun=["python", "test.py"],
                screenshots=["screen.png"],
                videos=["video.mp4"],
            )
        ],
    )
    return root / "manifest.json"


class FakeGitHub(GitHub):
    def __init__(self):
        self.current = TARGET
        self.assets, self.comments, self.releases, self.writes = [], [], [], []

    async def snapshot(self, target):
        return self.current

    async def api(self, path, *, method="GET", body=None, data=None, media_type=None, select=None):
        if method == "POST":
            self.writes.append((path, body))
            if path.endswith("/releases"):
                self.releases.append({"id": 1, "draft": False, **body})
                return self.releases[-1]
            if path.startswith("https://uploads.github.com/"):
                name = path.split("name=")[1]
                item = {
                    "name": name,
                    "size": len(data),
                    "state": "uploaded",
                    "browser_download_url": f"https://github.com/owner/repo/releases/download/evidence/{name}",
                }
                self.assets.append(item)
                return item
            if path.endswith("/comments"):
                item = {"body": body["body"], "html_url": TARGET.url + "#comment-1"}
                self.comments.append(item)
                return item
        elif "/comments?" in path:
            return self.comments
        elif "/assets?" in path:
            return self.assets
        elif "/releases?" in path:
            return self.releases
        raise AssertionError(path)


def test_uploads_only_evidence_and_deduplicates_repeat_publication(tmp_path):
    path = bundle(tmp_path)
    (tmp_path / ".env").write_text("PRIVATE=secret")
    github = FakeGitHub()

    async def run():
        first = await github.publish(path, TARGET)
        assert first == await github.publish(path, TARGET)

    asyncio.run(run())
    assert len(github.assets) == 2 and len(github.comments) == 1
    assert len(github.releases) == 1
    assert github.releases[0]["prerelease"] is True
    body = github.comments[0]["body"]
    assert "![Screenshot" in body and "[Playwright test" in body and "[video" not in body
    assert "secret" not in body


def test_publication_distinguishes_changed_behavior_and_blocked_smoke(tmp_path):
    path = bundle(tmp_path)
    data = json.loads(path.read_text())
    data['status'] = 'blocked'
    data['assumptions'] = ['GitHub is stubbed; production agents are untested.']
    data['tests'][0].update(title='SQLite proposal lifecycle', runner='terminal',
        coverage='changed_behavior', checks=['Approval survives reopen', 'Rejection removes queued work'])
    data['tests'].append({**data['tests'][0], 'case_id': 'smoke', 'title': 'Create dummy WorkOrder',
        'runner': 'playwright', 'coverage': 'regression', 'status': 'blocked',
        'detail': 'Creation succeeded; reload check did not run.',
        'checks': ['Same WorkOrder persists after reload']})
    path.write_text(json.dumps(data))
    github = FakeGitHub()
    asyncio.run(github.publish(path, TARGET))
    body = github.comments[0]['body']
    assert 'Result: **blocked**' in body
    assert 'Changed behavior; independent live behavior' in body
    assert 'Application regression smoke' in body
    assert 'Verified checks:' in body and 'Planned checks — not all verified:' in body
    assert 'Rejection removes queued work' in body
    assert 'production agents are untested' in body
    assert '[Backend test' in body


def test_publication_lists_passed_inconclusive_and_unexecuted_checkpoints(tmp_path):
    path = bundle(tmp_path)
    data = json.loads(path.read_text())
    data['status'] = 'blocked'
    data['tests'][0].update(status='blocked', checkpoints=[
        {'instruction': 'Prompt visible', 'status': 'passed', 'detail': 'Exact match'},
        {'instruction': 'Identity compared', 'status': 'blocked', 'detail': 'Missing baseline',
         'code': 'ASSERTION_INCONCLUSIVE'},
        {'instruction': 'Review after reload', 'status': 'blocked', 'detail': 'Not executed', 'code': 'NOT_RUN'}])
    path.write_text(json.dumps(data))
    github = FakeGitHub()
    asyncio.run(github.publish(path, TARGET))
    body = github.comments[0]['body']
    assert '**passed**: Prompt visible' in body
    assert '**inconclusive**: Identity compared' in body
    assert '**not run**: Review after reload' in body


@pytest.mark.parametrize("failure", ["head", "base", "oversize", "traversal", "dirty", "type"])
def test_rejects_invalid_or_stale_evidence_before_any_write(tmp_path, failure):
    path = bundle(tmp_path)
    data = json.loads(path.read_text())
    github = FakeGitHub()
    if failure == "head":
        github.current = PullRequest("owner/repo", 1, "c" * 40, BASE, BASE)
    if failure == "base":
        github.current = PullRequest("owner/repo", 1, HEAD, "c" * 40, BASE)
    if failure == "traversal":
        data["artifacts"][0]["path"] = "../private.py"
    if failure == "dirty":
        data["change"]["include_working_tree"] = True
    if failure == "type":
        data["artifacts"][0]["media_type"] = "video/mp4"
    if failure == "oversize":
        with (tmp_path / "screen.png").open("wb") as stream:
            stream.truncate(10_000_001)
        data["artifacts"][-1]["size_bytes"] = 10_000_001
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        asyncio.run(github.publish(path, TARGET))
    assert not github.writes


def test_skip_does_not_create_release_or_comment(tmp_path):
    github = FakeGitHub()
    assert asyncio.run(github.publish(bundle(tmp_path, "skipped"), TARGET)) is None
    assert not github.writes


@pytest.mark.parametrize('valid', [True, False])
def test_login_gif_is_validated_and_embedded_as_summary(tmp_path, valid):
    path = bundle(tmp_path)
    data = json.loads(path.read_text())
    gif = tmp_path / 'login-journey-summary.gif'
    gif.write_bytes(b'GIF89afixture' if valid else b'not-a-gif')
    data['artifacts'].append({'path': gif.name, 'type': 'screenshot', 'media_type': 'image/gif',
                              'size_bytes': gif.stat().st_size, 'case_id': 'C1'})
    path.write_text(json.dumps(data))
    github = FakeGitHub()
    if valid:
        asyncio.run(github.publish(path, TARGET))
        assert 'Journey summary' in github.comments[0]['body']
        assert any(a['name'].endswith('.gif') for a in github.assets)
    else:
        with pytest.raises(ValueError, match='Invalid media'):
            asyncio.run(github.publish(path, TARGET))
        assert not github.writes


def test_changed_pr_during_upload_never_receives_comment(tmp_path):
    github = FakeGitHub()
    original = github.api

    async def api(path, **kwargs):
        result = await original(path, **kwargs)
        if path.startswith("https://uploads.github.com/"):
            github.current = PullRequest("owner/repo", 1, "c" * 40, BASE, BASE)
        return result

    github.api = api
    with pytest.raises(ValueError, match="changed"):
        asyncio.run(github.publish(bundle(tmp_path), TARGET))
    assert not github.comments


def test_retry_saved_bundle_reuses_uploaded_assets_and_checks_revision(tmp_path):
    from dataclasses import asdict

    from open_verify.github import publish_saved
    bundle(tmp_path)
    (tmp_path / 'pull-request.json').write_text(json.dumps(asdict(TARGET)))
    github = FakeGitHub()
    api = github.api
    fail_once = True

    async def flaky(path, **kwargs):
        nonlocal fail_once
        if path.startswith('https://uploads.github.com') and len(github.assets) == 1 and fail_once:
            fail_once = False
            raise RuntimeError('Upload interrupted')
        return await api(path, **kwargs)

    github.api = flaky
    assert asyncio.run(publish_saved(tmp_path, github=github)) == 2
    assert len(github.assets) == 1 and not github.comments
    assert asyncio.run(publish_saved(tmp_path, github=github)) == 0
    assert len(github.assets) == 2 and len(github.comments) == 1
    github.current = PullRequest('owner/repo', 1, 'c' * 40, BASE, BASE)
    assert asyncio.run(publish_saved(tmp_path, github=github)) == 2
    assert len(github.comments) == 1


def test_starter_asset_from_failed_upload_is_replaced(tmp_path):
    github = FakeGitHub()
    path = bundle(tmp_path)
    asyncio.run(github.publish(path, TARGET))
    github.assets[0].update(state='starter', size=0, id=99)
    api = github.api
    removed = []

    async def deleting(path, **kwargs):
        if kwargs.get('method') == 'DELETE':
            assert path.endswith('/releases/assets/99')
            github.assets.pop(0)
            removed.append(path)
            return None
        return await api(path, **kwargs)

    github.api = deleting
    asyncio.run(github.publish(path, TARGET))
    assert len(removed) == 1 and len(github.assets) == 2
    assert len(github.comments) == 1


def test_real_gh_upload_uses_content_length(tmp_path, monkeypatch):
    import shutil
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    if not shutil.which('gh'):
        pytest.skip('gh is not installed')
    received = []
    class Upload(BaseHTTPRequestHandler):
        def do_POST(self):
            length = self.headers.get('Content-Length')
            received.append((length, self.headers.get('Transfer-Encoding'),
                             self.rfile.read(int(length or 0))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *_):
            pass

    # No host credential or GitHub connection is used by this local wire-level test.
    monkeypatch.setenv('GH_TOKEN', 'test-only-placeholder')
    monkeypatch.setenv('GH_CONFIG_DIR', str(tmp_path))
    monkeypatch.setenv('GH_NO_UPDATE_NOTIFIER', '1')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Upload)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    data = b'\x89PNG\x00\xfffixture'
    try:
        result = asyncio.run(GitHub().api(
            f'http://127.0.0.1:{server.server_port}/assets', method='POST',
            data=data, media_type='image/png'))
        assert result == {'ok': True}
        assert received == [(str(len(data)), None, data)]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
