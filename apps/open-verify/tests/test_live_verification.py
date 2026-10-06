"""Existing CI tests must not silently replace independent behavioral verification."""

import asyncio
import json
import sys

import pytest
from test_backend_suites import command, planned, suite
from test_change_workflow import browser_plan, execute, impact, journey
from test_workflow import ScriptedAgent, action

from open_verify.artifacts import Artifacts
from open_verify.cli import parser
from open_verify.runner import VerificationRunner
from open_verify.tools import LocalTools
from open_verify.verification_policy import existing_test_command


@pytest.mark.parametrize("argv", [
    [".venv/bin/python", "-m", "pytest", "tests/test_scoped_tickets.py::test_lifecycle"],
    ["uv", "run", "--locked", "pytest"],
    [".venv/bin/pytest", "tests"],
    [r".venv\Scripts\pytest.exe", "tests"],
    ["sh", "-c", "python -m pytest; echo done"],
    ["sh", "-c", "python -m pytest tests"],
    ["python", "-c", "import pytest as p; p.main(['tests'])"],
    ["python", "-m", "unittest", "discover"],
    ["npm", "--prefix", "apps/web", "run", "test:e2e"],
    ["npx", "playwright", "test"],
    ["pnpm", "exec", "vitest", "run"],
])
def test_recognizes_existing_test_launchers(argv):
    assert existing_test_command(argv)


@pytest.mark.parametrize("argv", [
    ["python", "-c", "import sqlite3; assert sqlite3.connect(':memory:')"],
    ["uv", "run", "engine-web", "--config", "qa.toml"],
    ["node", "app.js"],
    ["python", "independent_check.py"],
])
def test_allows_direct_behavior_checks_and_app_startup(argv):
    assert not existing_test_command(argv)


def test_cli_defaults_to_live_with_explicit_existing_tests_option():
    assert parser().parse_args([]).verification == "live"
    assert parser().parse_args(["--verification", "tests"]).verification == "tests"


def test_rejected_pytest_wrapper_can_be_replaced_with_an_independent_check(tmp_path):
    reused = suite("terminal", [{
        "kind": "command", "argv": [sys.executable, "-m", "pytest", "--version"],
        "expect": {"exit_code": 0},
    }])
    independent = suite("terminal", [command(
        "import sqlite3; "
        "c=sqlite3.connect(':memory:'); "
        "assert c.execute('select 42').fetchone() == (42,); print('hello')"
    )])
    agent = ScriptedAgent([
        planned("terminal"), action("run_backend_test", **reused.model_dump()),
        action("run_backend_test", **independent.model_dump()),
    ])
    artifacts = Artifacts(tmp_path / "runs")
    engine = LocalTools(tmp_path, artifacts, allow_exec=True)
    runner = VerificationRunner(agent, engine, artifacts, progress=lambda _: None)

    async def run():
        try:
            return await runner.run("Check the SQLite API independently")
        finally:
            await engine.close()

    report = asyncio.run(run())
    assert report["findings"][0]["status"] == "passed"
    refused = next(e for e in artifacts.observations if e["tool"] == "run_backend_test")
    assert not refused["ok"] and "cannot establish live behavior" in refused["result"]["error"]
    commands = [e["arguments"]["argv"] for e in artifacts.observations if e["tool"] == "run_command"]
    assert len(commands) == 1 and "pytest" not in commands[0]
    assert "Independent live behavior" in (artifacts.path / "report.md").read_text()


@pytest.mark.parametrize("mode,accepted", [("live", False), ("tests", True)])
def test_existing_test_plan_requires_explicit_mode(tmp_path, mode, accepted):
    plan = planned("terminal")
    plan["plan"]["cases"][0]["verification"] = "existing_tests"
    artifacts = Artifacts(tmp_path / "runs")
    runner = VerificationRunner(
        ScriptedAgent([plan, {"kind": "finish", "note": "No live case"}]),
        LocalTools(tmp_path, artifacts), artifacts, plan_only=True,
        verification=mode, progress=lambda _: None,
    )
    report = asyncio.run(runner.run("Check existing tests"))
    assert (report["status"] == "planned") is accepted
    if accepted:
        assert "Existing repository tests" in (artifacts.path / "report.md").read_text()


def test_explicit_existing_test_case_can_execute(tmp_path):
    (tmp_path / "test_existing.py").write_text("def test_contract(): assert 2 + 2 == 4\n")
    plan = planned("terminal")
    plan["plan"]["cases"][0]["verification"] = "existing_tests"
    existing = suite("terminal", [{
        "kind": "command",
        "argv": [sys.executable, "-m", "pytest", "-q", "test_existing.py"],
        "expect": {"exit_code": 0, "output": {"mode": "contains", "value": "1 passed"}},
    }])
    artifacts = Artifacts(tmp_path / "runs")
    engine = LocalTools(tmp_path, artifacts, allow_exec=True)
    runner = VerificationRunner(
        ScriptedAgent([plan, action("run_backend_test", **existing.model_dump())]),
        engine, artifacts, verification="tests", progress=lambda _: None,
    )

    async def run():
        try:
            return await runner.run("Recheck the existing test")
        finally:
            await engine.close()

    report = asyncio.run(run())
    assert report["findings"][0]["status"] == "passed"
    assert "Existing repository tests" in (artifacts.path / "report.md").read_text()
    assert "no independent live coverage" in report["findings"][0]["actual"]


def test_backend_pr_browser_smoke_captures_media(tmp_path):
    report, browser, _, _ = execute(tmp_path, [
        impact(ui=False), browser_plan(), journey(),
    ])
    assert report["findings"][0]["status"] == "passed"
    assert browser.calls[0][1] is True


def test_backend_pr_browser_media_is_publishable(tmp_path):
    from open_verify.manifest import write_manifest
    from open_verify.test_spec import TestResult

    (tmp_path / "test.py").write_text("pass\n")
    (tmp_path / "smoke.gif").write_bytes(b"GIF89afixture")
    result = TestResult(case_id="smoke", status="passed", detail="Created task",
                        test_file="test.py", rerun=["python", "test.py"],
                        screenshots=["smoke.gif"])
    report = {"status": "complete", "findings": [{
        "case_id": "smoke", "status": "passed", "actual": "Created task",
    }], "impact": impact(ui=False)["impact"]}
    write_manifest(tmp_path, report, None, [result])
    saved = json.loads((tmp_path / "manifest.json").read_text())
    assert [a["path"] for a in saved["artifacts"]] == ["test.py", "smoke.gif"]
