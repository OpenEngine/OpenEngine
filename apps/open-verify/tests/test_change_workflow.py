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
                checks={"Hello Ada": [0]},
            ),
            # Claiming the opposite cannot change the observed test status.
            result("failed" if status == "passed" else "passed"),
            result(status),
        ],
        runner_status=status,
    )
    if status == "passed":
        assert len(agent.prompts) == 3  # Host finishes immediately, without another model call.
    else:
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


def journey(**updates):
    return action("run_browser_test", **{
        "case_id": "greet", "url": "http://localhost",
        "steps": [{"kind": "expect_text", "text": "Hello Ada"}],
        "checks": {"Hello Ada": [0]}, **updates,
    })


def test_rejects_scope_expansion_and_stops_after_complete_journey(tmp_path):
    expanded = browser_plan()
    expanded['plan']['cases'].append(dict(expanded['plan']['cases'][0], id='extra'))
    report, runner, _, agent = execute(tmp_path, [impact(), expanded, browser_plan(), journey()])
    assert 'max_cases=1' in agent.prompts[2]
    assert len(runner.calls) == 1
    assert report['status'] == 'complete'


def test_partial_coverage_and_container_click_do_not_execute(tmp_path):
    _, runner, _, agent = execute(tmp_path, [
        impact(), browser_plan(), journey(checks={}),
        journey(steps=[{'kind': 'click', 'locator': {'by': 'role', 'role': 'group', 'name': 'User'}},
                       {'kind': 'expect_text', 'text': 'Hello Ada'}], checks={'Hello Ada': [1]}),
        journey(),
    ])
    assert len(runner.calls) == 1
    assert 'every planned completion check' in agent.prompts[3]
    assert 'interactive control' in agent.prompts[4]


@pytest.mark.parametrize('status', ['failed', 'blocked'])
def test_only_one_diagnosed_retry_is_allowed(tmp_path, status):
    report, runner, _, agent = execute(tmp_path, [
        impact(), browser_plan(), journey(), journey(),
        journey(retry_reason='The observed label changed; corrected the locator with the same expectation.'),
    ], runner_status=status)
    assert 'Retry requires a diagnosis' in agent.prompts[-1]
    assert len(runner.calls) == 2
    assert report['status'] == 'complete'
    assert report['findings'][0]['status'] == status


def test_screenshots_cannot_count_as_completion_assertions(tmp_path):
    _, runner, _, agent = execute(tmp_path, [
        impact(), browser_plan(),
        journey(steps=[{'kind': 'screenshot', 'name': 'page'}, {'kind': 'expect_text', 'text': 'Hello Ada'}]),
        journey(),
    ])
    assert 'must reference assertion steps' in agent.prompts[-1]
    assert len(runner.calls) == 1


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


def test_discovery_preserves_changed_paths_and_marks_patch_preview(tmp_path):
    class CaptureAgent:
        prompt = None
        async def decide(self, prompt):
            self.prompt = prompt
            return Decision.model_validate({'kind': 'finish', 'note': 'Captured context'})
    artifacts = Artifacts(tmp_path / 'runs')
    agent = CaptureAgent()
    files = [f'file-{i}.py' for i in range(90)] + ['login.py']
    verification = Verification(
        agent, LocalTools(tmp_path, artifacts), artifacts,
        change=Change(base='base', head='head', files=files, diff='x' * 50000),
        progress=lambda _: None,
    )
    asyncio.run(verification.run('Check login and validation errors'))
    context = json.loads(agent.prompt.split('\n')[-1])
    assert context['change']['files'] == files
    assert context['change']['truncated'] is True
    assert len(context['change']['diff']) == 40000
    assert 'incomplete diff attribution alone does not' in agent.prompt
