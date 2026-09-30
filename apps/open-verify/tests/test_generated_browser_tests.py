import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
import test_tools
from test_change_workflow import browser_plan, impact, result
from test_workflow import ScriptedAgent, action

from open_verify.artifacts import Artifacts
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
        steps=[
            {"kind": "fill", "locator": {"by": "label", "name": "Name"}, "value": "Ada"},
            {"kind": "click", "locator": {"by": "role", "role": "button", "name": "Greet"}},
            {"kind": "expect_text", "text": expected},
        ],
    )


test_spec.__test__ = False


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
    tools = LocalTools(tmp_path, artifacts, headless=True)
    runner = PlaywrightRunner(tmp_path, artifacts)
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

    report = asyncio.run(run())
    assert report["findings"][0]["status"] == status
    manifest = json.loads((artifacts.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == status
    assert manifest["tests"][0]["status"] == status
    assert {item["type"] for item in manifest["artifacts"]} >= {"test", "screenshot"}
    recordings = list(artifacts.path.rglob("*.webm"))
    assert recordings and all(path.stat().st_size > 0 for path in recordings)
    for item in manifest["artifacts"]:
        assert (artifacts.path / item["path"]).is_file()
        if item["type"] == "video":
            assert item["size_bytes"] < 10_000_000
            assert (artifacts.path / item["path"]).read_bytes()[4:8] == b"ftyp"
    assert any(item["type"] == "video" for item in manifest["artifacts"]) or any(
        "Video omitted" in reason for reason in manifest["omissions"]
    )
    if shutil.which("ffmpeg"):
        assert any(item["type"] == "video" for item in manifest["artifacts"]), manifest["omissions"]

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
            assert Path(source).is_file()  # The browser already finalized the recording.
            encoding.set()
            await asyncio.Future()

        monkeypatch.setattr("open_verify.playwright_runner.encode_video", encoder)
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
        "Video omitted" in reason and "interrupted" in reason for reason in manifest["omissions"]
    )
    for item in manifest["artifacts"]:
        assert (artifacts.path / item["path"]).is_file()
    (receipt,) = artifacts.path.glob("executions/*/*/result.json")
    saved = json.loads(receipt.read_text(encoding="utf-8"))
    assert saved["omissions"] == manifest["omissions"]
