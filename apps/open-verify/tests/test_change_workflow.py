import asyncio
import json

import pytest
from test_changes import GitFixture
from test_workflow import ScriptedAgent, action

from open_verify.artifacts import Artifacts
from open_verify.changes import Change, read_change
from open_verify.models import Decision
from open_verify.test_spec import TestResult as RunnerResult
from open_verify.tools import LocalTools
from open_verify.workflow import Verification, exit_code


def impact(decision="verify", *, ui=True):
    return {
        "kind": "impact",
        "impact": {
            "decision": decision,
            "reason": "Greeting behavior changed",
            "material_ui_change": ui,
            "affected_files": ["app.html"],
            "journeys": ["Greet a user"] if decision == "verify" else [],
        },
    }


def browser_plan():
    return {
        "kind": "plan",
        "plan": {
            "project_summary": "Greeting UI",
            "startup": [],
            "cases": [
                {
                    "id": "greet",
                    "title": "Greet user",
                    "interface": "browser",
                    "steps": ["Open page and greet"],
                    "expected": "Hello Ada",
                }
            ],
        },
    }


def result(status="passed", evidence="E0001"):
    return {
        "kind": "finding",
        "finding": {
            "case_id": "greet",
            "status": status,
            "actual": "Observed greeting",
            "evidence": [evidence],
            "reproduction": ["Open page"] if status == "failed" else [],
        },
    }


class Runner:
    def __init__(self, artifacts, status="passed"):
        self.artifacts, self.status = artifacts, status
        self.calls = []

    async def run(self, test, *, capture_media, on_result=None):
        self.calls.append((test, capture_media))
        (self.artifacts.path / "test.py").write_text("# fixture test", encoding="utf-8")
        return RunnerResult(
            case_id=test.case_id,
            status=self.status,
            detail="Fixture execution",
            test_file="test.py",
            rerun=["python", "test.py"],
        )


def execute(tmp_path, decisions, *, runner_status="passed", **options):
    artifacts = Artifacts(tmp_path / "runs")
    tools = LocalTools(tmp_path, artifacts)
    agent = ScriptedAgent(decisions)
    runner = Runner(artifacts, runner_status)
    verify = Verification(
        agent,
        tools,
        artifacts,
        change=Change(base="base", head="head", files=["app.html"]),
        test_runner=runner,
        progress=lambda _: None,
        **options,
    )
    report = asyncio.run(verify.run("Verify change"))
    manifest = json.loads((artifacts.path / "manifest.json").read_text(encoding="utf-8"))
    return report, runner, manifest, agent


def test_no_material_behavior_change_skips_without_tests_or_attachments(tmp_path):
    report, runner, manifest, _ = execute(tmp_path, [impact("skip", ui=False)])
    assert exit_code(report) == 0
    assert manifest["status"] == "skipped"
    assert manifest["artifacts"] == manifest["tests"] == runner.calls == []


def test_uncertain_impact_is_blocked_not_skipped(tmp_path):
    report, _, manifest, _ = execute(tmp_path, [impact("uncertain", ui=False)])
    assert exit_code(report) == 2
    assert manifest["status"] == "blocked"


def test_plan_only_requires_impact_and_does_not_run_browser(tmp_path):
    report, runner, manifest, agent = execute(
        tmp_path,
        [browser_plan(), impact(), browser_plan()],
        plan_only=True,
    )
    assert "Assess the change's impact" in agent.prompts[1]
    assert report["status"] == manifest["status"] == "planned"
    assert runner.calls == []


@pytest.mark.parametrize("status", ["passed", "failed", "blocked"])
def test_host_test_result_controls_findings_and_manifest(tmp_path, status):
    report, runner, manifest, agent = execute(
        tmp_path,
        [
            impact(),
            browser_plan(),
            action(
                "run_browser_test",
                case_id="greet",
                url="http://localhost:8000",
                steps=[{"kind": "expect_text", "text": "Hello Ada"}],
            ),
            # Claiming the opposite cannot change the observed test status.
            result("failed" if status == "passed" else "passed"),
            result(status),
        ],
        runner_status=status,
    )
    assert "match its actual status" in agent.prompts[-1]
    assert report["findings"][0]["status"] == status
    assert runner.calls[0][1] is True
    assert manifest["tests"][0]["status"] == status
    assert manifest["artifacts"][0]["case_id"] == "greet"


def test_unknown_case_is_rejected_before_runner(tmp_path):
    _, runner, manifest, _ = execute(
        tmp_path,
        [
            impact(),
            browser_plan(),
            action(
                "run_browser_test",
                case_id="not-planned",
                url="http://localhost",
                steps=[{"kind": "expect_text", "text": "hello"}],
            ),
            {"kind": "finish", "note": "No test executed"},
        ],
    )
    assert runner.calls == []
    assert manifest["status"] == "incomplete"


def test_skipping_material_ui_change_is_invalid():
    with pytest.raises(ValueError, match="cannot be skipped"):
        Decision.model_validate(impact("skip"))


def test_binary_only_change_cannot_produce_a_skipped_manifest(tmp_path):
    git = GitFixture(
        files="logo.png\0",
        numstat="-\t-\tlogo.png\0",
        diff="Binary files a/logo.png and b/logo.png differ\n",
    )
    artifacts = Artifacts(tmp_path / "runs")
    decisions = [impact("skip", ui=False), impact("uncertain", ui=False)]
    for decision in decisions:
        decision["impact"]["affected_files"] = ["logo.png"]
    agent = ScriptedAgent(decisions)

    async def run():
        change = await read_change(tmp_path, "main", reader=git)
        return await Verification(
            agent,
            LocalTools(tmp_path, artifacts),
            artifacts,
            change=change,
            progress=lambda _: None,
        ).run("Verify logo change")

    report = asyncio.run(run())
    assert report["status"] == "blocked"
    assert "Change inspection is incomplete" in agent.prompts[-1]
    manifest = json.loads((artifacts.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "blocked"
    assert manifest["change"]["truncated"]
