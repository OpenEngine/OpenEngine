import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
import test_tools
from test_change_workflow import browser_plan, impact, result
from test_workflow import ScriptedAgent, action

from open_verify.artifacts import Artifacts
from open_verify.browser_session import BrowserSession
from open_verify.changes import Change
from open_verify.playwright_runner import PlaywrightRunner
from open_verify.test_codegen import render_test
from open_verify.test_spec import BrowserTest
from open_verify.tools import LocalTools
from open_verify.workflow import Verification

web_app = test_tools.web_app


def test_spec(url, *, expected="Hello Ada"):
    return BrowserTest(
        case_id="greet",
        url=url,
        checks={"Hello Ada": [3]},
        steps=[
            {"kind": "screenshot", "name": "initial"},
            {"kind": "fill", "locator": {"by": "label", "name": "Name"}, "value": "Ada"},
            {"kind": "click", "locator": {"by": "role", "role": "button", "name": "Greet"}},
            {"kind": "expect_text", "text": expected},
            {"kind": "screenshot", "name": "greeting"},
        ],
    )


test_spec.__test__ = False


def test_click_highlight_is_visible_in_capture_and_removed_before_action(tmp_path, web_app, monkeypatch):
    from playwright.async_api import Page

    from open_verify.capture import CheckpointCapture

    original = Page.screenshot
    captured = []

    async def screenshot(page, **kwargs):
        marker = page.locator('[data-ov-click-highlight]')
        assert await marker.count() == 1
        assert await marker.inner_text() == 'Next click'
        assert await marker.evaluate('(e) => getComputedStyle(e).pointerEvents') == 'none'
        assert await marker.evaluate('(e) => getComputedStyle(e).borderTopColor') == 'rgb(249, 115, 22)'
        captured.append(True)
        return await original(page, **kwargs)

    monkeypatch.setattr(Page, 'screenshot', screenshot)

    async def run():
        tools = LocalTools(tmp_path, Artifacts(tmp_path / 'runs'), headless=True)
        try:
            page = await tools.browser_page()
            await page.goto(web_app)
            button = page.get_by_role('button', name='Greet')
            before = await button.get_attribute('style')
            capture = CheckpointCapture(tools.artifacts.path / 'checkpoints')
            await capture(page, 'before-click', button)
            assert captured and len(capture.paths) == 1 and not capture.omissions
            assert await page.locator('[data-ov-click-highlight]').count() == 0
            assert await button.get_attribute('style') == before
            await page.get_by_label('Name').fill('Ada')
            await button.click()
            assert await page.get_by_text('Hello Ada', exact=True).is_visible()
        finally:
            await tools.close()

    asyncio.run(run())


def test_legacy_generated_journey_still_replays(tmp_path, web_app, monkeypatch):
    from open_verify.playwright_runner import replay_main

    async def legacy(page, *, entry_url=None):
        await page.goto(entry_url or web_app)
        assert await page.get_by_role('button', name='Greet').is_visible()

    monkeypatch.setattr(sys, 'argv', ['legacy.py', '--output', str(tmp_path / 'replay')])
    with pytest.raises(SystemExit) as result:
        replay_main(legacy, url=web_app, timeout=15)
    assert result.value.code == 0


def test_checkpoint_dedup_keeps_return_to_previous_screen(tmp_path):
    from open_verify.playwright_runner import unique_screenshots

    paths = []
    for index, data in enumerate((b'initial', b'initial', b'changed', b'initial')):
        path = tmp_path / f'{index}.png'
        path.write_bytes(data)
        paths.append(path)
    assert unique_screenshots(paths) == [paths[0], paths[2], paths[3]]


def test_complete_cart_lifecycle_exports_one_test_and_one_gif(tmp_path, web_app):
    artifacts = Artifacts(tmp_path / 'runs')
    definition = browser_plan()
    case = definition['plan']['cases'][0]
    case.update(title='Add and remove an item', expected='Cart is empty again',
                checks=['Initially empty', 'Item added', 'Item removed'])
    journey = BrowserTest(case_id='greet', url=web_app + '/cart', steps=[
        {'kind': 'expect_text', 'text': 'Cart: 0'},
        {'kind': 'click', 'locator': {'by': 'role', 'role': 'button', 'name': 'Add item'}},
        {'kind': 'expect_text', 'text': 'Cart: 1'},
        {'kind': 'click', 'locator': {'by': 'role', 'role': 'button', 'name': 'Remove item'}},
        {'kind': 'expect_text', 'text': 'Cart: 0'},
    ], checks={'Initially empty': [0], 'Item added': [2], 'Item removed': [4]})
    agent = ScriptedAgent([impact(), definition, action('run_browser_test', **journey.model_dump())])
    runner = PlaywrightRunner(tmp_path, artifacts)
    verification = Verification(agent, LocalTools(tmp_path, artifacts), artifacts,
        change=Change(base='a', head='b', files=['app.html']), test_runner=runner, progress=lambda _: None)
    report = asyncio.run(verification.run('Test adding and removing a cart item'))
    assert report['status'] == 'complete' and runner.attempt == 1
    manifest = json.loads((artifacts.path / 'manifest.json').read_text())
    assert len(manifest['tests']) == 1
    assert [item['media_type'] for item in manifest['artifacts']] == ['text/x-python', 'image/gif']
    assert len(list(artifacts.path.rglob('*.png'))) >= 4


@pytest.mark.parametrize('value,status', [(2, 'passed'), (3, 'failed')])
def test_reload_navigation_and_structured_json_in_one_journey(tmp_path, web_app, value, status):
    runner = PlaywrightRunner(tmp_path, Artifacts(tmp_path / 'runs'))
    progress = []
    runner.progress = progress.append
    test = BrowserTest(case_id='api', url=web_app, steps=[
        {'kind': 'reload'},
        {'kind': 'navigate', 'path': '/other'},
        {'kind': 'expect_text', 'text': 'Greet'},
        {'kind': 'expect_json', 'path': '/api', 'field': ['items', 1], 'value': value},
    ], checks={'App accessible after reload': [2], 'API data is correct': [3]})
    result = asyncio.run(runner.run(test, capture_media=False))
    assert result.status == status
    assert len(progress) == 4
    assert 'API data is correct' in progress[-1]


@pytest.mark.parametrize('path', ['//example.com', '/\\example.com', '/\n/example.com', 'https://example.com'])
@pytest.mark.parametrize('kind', ['navigate', 'expect_json'])
def test_new_steps_cannot_escape_app_origin(path, kind):
    step = {'kind': kind, 'path': path}
    if kind == 'expect_json':
        step['value'] = True
    with pytest.raises(ValueError):
        BrowserTest(case_id='scope', url='http://localhost', steps=[
            step, {'kind': 'expect_text', 'text': 'app'},
        ])


def test_compiler_treats_model_strings_as_literals_and_requires_assertions():
    text = "'); __import__('os').system('not-a-command') #\nquoted"
    source = render_test(test_spec("http://localhost", expected=text))
    namespace = {"__name__": "fixture"}
    pytest.importorskip("playwright.async_api")
    exec(compile(source, "generated.py", "exec"), namespace)
    assert callable(namespace["test_change"])
    assert repr(text) in source
    with pytest.raises(ValueError, match="explicit assertion"):
        BrowserTest(
            case_id="a",
            url="http://localhost",
            steps=[
                {"kind": "click", "locator": {"by": "text", "name": "Go"}},
            ],
        )


@pytest.mark.parametrize("expected, status", [("Hello Ada", "passed"), ("Hello Grace", "failed")])
def test_change_runs_exact_generated_test_and_retains_focused_evidence(
    tmp_path, web_app, expected, status, monkeypatch
):
    pytest.importorskip("playwright.async_api")
    artifacts = Artifacts(tmp_path / "runs")
    session = BrowserSession(headless=True)
    tools = LocalTools(tmp_path, artifacts, headless=True, browser_session=session)
    runner = PlaywrightRunner(tmp_path, artifacts, browser_session=session)
    test = test_spec(web_app, expected=expected)
    # A replay-only override must not silently change the app under verification.
    monkeypatch.setenv("OV_BASE_URL", "file:///not-a-project-path")
    agent = ScriptedAgent(
        [
            impact(),
            browser_plan(),
            action("run_browser_test", **test.model_dump()),
            result(status),
        ]
    )

    async def run():
        try:
            report = await Verification(
                agent,
                tools,
                artifacts,
                change=Change(base="a", head="b", files=["app.html"]),
                test_runner=runner,
                progress=lambda _: None,
            ).run("Verify greeting")
            return report
        finally:
            await tools.close()
            assert await session.close() == []

    report = asyncio.run(run())
    assert report["findings"][0]["status"] == status
    manifest = json.loads((artifacts.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == status
    assert manifest["tests"][0]["status"] == status
    assert {item["type"] for item in manifest["artifacts"]} >= {"test", "screenshot"}
    checkpoint_images = list(artifacts.path.rglob("checkpoint-*.png"))
    assert len(checkpoint_images) == (5 if status == "passed" else 4)
    for screenshot in checkpoint_images:
        assert screenshot.read_bytes().startswith(b"\x89PNG")
    assert not list(artifacts.path.rglob("*.webm"))
    assert not list(artifacts.path.rglob("*.mp4"))
    assert all(item['type'] != 'video' for item in manifest['artifacts'])
    gifs = [item for item in manifest['artifacts'] if item['media_type'] == 'image/gif']
    assert len(gifs) == 1, manifest['omissions']
    assert gifs[0]['size_bytes'] < 10_000_000
    assert (artifacts.path / gifs[0]['path']).read_bytes().startswith(b'GIF89a')

    # Rerun the exported Python file as a separate process, not the in-process
    # host runner. It must reproduce the result against the same live fixture.
    async def replay(*, override=None):
        argv = manifest["tests"][0]["rerun"]
        environment = dict(os.environ)
        environment.pop("OV_BASE_URL", None)
        if override:
            environment["OV_BASE_URL"] = override
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            *argv[1:],
            cwd=artifacts.path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=environment,
            **({"creationflags": 0x08000000} if os.name == "nt" else {}),
        )
        output, _ = await asyncio.wait_for(process.communicate(), 60)
        expected_code = 2 if override else (0 if status == "passed" else 1)
        assert process.returncode == expected_code, output.decode()
        if override:
            assert "Use an HTTP(S) URL" in output.decode()

    asyncio.run(replay())
    if status == "passed":
        asyncio.run(replay(override="file:///not-a-project-path"))


def test_navigation_failure_is_blocked_and_capture_can_be_disabled(tmp_path):
    pytest.importorskip("playwright.async_api")
    artifacts = Artifacts(tmp_path / "runs")
    runner = PlaywrightRunner(tmp_path, artifacts)
    # Port zero is guaranteed not to be an app listener.
    result = asyncio.run(runner.run(test_spec("http://127.0.0.1:0"), capture_media=False))
    assert result.status == "blocked"
    assert result.screenshots == result.videos == []
    assert not list(artifacts.path.rglob("*.webm"))


@pytest.mark.parametrize("expected, status", [("Hello Ada", "passed"), ("Hello Grace", "failed")])
def test_cancelling_encoding_preserves_completed_evidence(
    tmp_path, web_app, monkeypatch, expected, status
):
    pytest.importorskip("playwright.async_api")
    artifacts = Artifacts(tmp_path / "runs")
    tools = LocalTools(tmp_path, artifacts, headless=True)
    test = test_spec(web_app, expected=expected)
    verification = Verification(
        ScriptedAgent([impact(), browser_plan(), action("run_browser_test", **test.model_dump())]),
        tools,
        artifacts,
        change=Change(base="a", head="b", files=["app.html"]),
        test_runner=PlaywrightRunner(tmp_path, artifacts),
        progress=lambda _: None,
    )

    async def run():
        encoding = asyncio.Event()

        async def encoder(source, destination):
            assert source and all(Path(p).is_file() for p in source)  # Checkpoints exist before encoding.
            encoding.set()
            await asyncio.Future()

        monkeypatch.setattr("open_verify.playwright_runner.encode_gif", encoder)
        task = asyncio.create_task(verification.run("Verify greeting"))
        try:
            await asyncio.wait_for(encoding.wait(), 30)
            # Evidence is available before optional media processing completes.
            assert len(verification.test_results) == 1
            assert verification.test_results[0].status == status
            receipts = list(artifacts.path.glob("executions/*/*/result.json"))
            assert len(receipts) == 1
            saved = json.loads(receipts[0].read_text(encoding="utf-8"))
            assert saved["status"] == status and saved["screenshots"]
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await tools.close()

    asyncio.run(run())
    manifest = json.loads((artifacts.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "incomplete"
    assert len(manifest["tests"]) == 1
    assert manifest["tests"][0]["status"] == status
    assert {item["type"] for item in manifest["artifacts"]} == {"test", "screenshot"}
    assert any(
        "GIF omitted" in reason and "interrupted" in reason for reason in manifest["omissions"]
    )
    for item in manifest["artifacts"]:
        assert (artifacts.path / item["path"]).is_file()
    (receipt,) = artifacts.path.glob("executions/*/*/result.json")
    saved = json.loads(receipt.read_text(encoding="utf-8"))
    assert all(reason in manifest["omissions"] for reason in saved["omissions"])


@pytest.mark.parametrize('match,expected_status', [('exact', 'failed'), ('contains', 'passed')])
def test_explicit_fragment_matching_agrees_in_live_checks_and_export(tmp_path, web_app, match, expected_status):
    from open_verify.test_spec import ExpectText

    artifacts = Artifacts(tmp_path / 'runs')
    engine = LocalTools(tmp_path, artifacts, headless=True)
    check = ExpectText(kind='expect_text', text='Ada', match=match)
    test = test_spec(web_app)
    test.steps[3] = check
    namespace = {'__name__': 'generated_test'}
    exec(compile(render_test(test), 'generated_test.py', 'exec'), namespace)

    async def run():
        try:
            page = await engine.browser_page()
            await page.goto(web_app)
            await page.get_by_label('Name').fill('Ada')
            await page.get_by_role('button', name='Greet').click()
            receipt = await engine.assert_check(check)
            assert receipt['result']['status'] == expected_status
            if expected_status == 'failed':
                with pytest.raises(AssertionError):
                    await namespace['test_change'](page, progress=lambda _: None)
            else:
                await namespace['test_change'](page, progress=lambda _: None)
        finally:
            await engine.close()

    asyncio.run(run())
