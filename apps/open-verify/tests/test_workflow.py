import asyncio
import json
import sys
from pathlib import Path

import pytest

from open_verify.agent import ACPDecisionAgent, parse_decision, provider_for
from open_verify.artifacts import Artifacts
from open_verify.models import Decision
from open_verify.tools import LocalTools
from open_verify.workflow import Verification, exit_code


def plan(*, questions=None):
    return {
        "kind": "plan",
        "plan": {
            "project_summary": "A CLI greeting app",
            "startup": [],
            "questions": questions or [],
            "cases": [
                {
                    "id": "greeting",
                    "title": "Print a greeting",
                    "interface": "terminal",
                    "steps": ["Run the greeting command"],
                    "expected": "Prints hello and exits 0",
                }
            ],
        },
    }


def finding(status="passed", evidence=None):
    return {
        "kind": "finding",
        "finding": {
            "case_id": "greeting",
            "status": status,
            "actual": "Printed hello",
            "evidence": evidence or [],
            "reproduction": ["Run the command"] if status == "failed" else [],
        },
    }


def action(tool, **arguments):
    return {
        "kind": "action",
        "action": {"tool": tool, "arguments": arguments, "reason": "Inspect the app"},
    }


def question(text="Do you normally start a local dependency before this app?", evidence="E0001"):
    return {"kind": "question", "question": {"text": text, "evidence": [evidence]}}


class ScriptedAgent:
    def __init__(self, decisions):
        self.decisions = iter(decisions)
        self.prompts = []

    async def decide(self, prompt):
        self.prompts.append(prompt)
        return Decision.model_validate(next(self.decisions))


def execute(tmp_path, decisions, *, allow_exec=True, **options):
    artifacts = Artifacts(tmp_path / "runs")
    tools = LocalTools(tmp_path, artifacts, allow_exec=allow_exec)
    agent = ScriptedAgent(decisions)

    async def run():
        try:
            report = await Verification(
                agent, tools, artifacts, progress=lambda _: None, **options
            ).run("Test greeting")
            return report, agent, artifacts
        finally:
            await tools.close()

    return asyncio.run(run())


def test_real_graph_rejects_invented_evidence_then_accepts_actual_output(tmp_path):
    report, agent, artifacts = execute(
        tmp_path,
        [
            plan(),
            finding(evidence=["E9999"]),
            action("run_command", argv=[sys.executable, "-c", "print('hello')"]),
            finding(evidence=["E0001"]),
        ],
    )
    assert report["status"] == "complete"
    assert exit_code(report) == 0
    assert "unknown evidence" in agent.prompts[2]
    evidence = json.loads((artifacts.path / "evidence.jsonl").read_text())
    assert evidence["result"]["exit_code"] == 0
    assert "hello" in evidence["result"]["output"]
    assert "greeting: passed" in (artifacts.path / "report.md").read_text()
    assert "actions/E0001.json" in (artifacts.path / "report.md").read_text()
    case_receipt = json.loads((artifacts.path / "cases" / "greeting.json").read_text())
    assert case_receipt["finding"]["case_id"] == "greeting"
    assert case_receipt["evidence"][0]["id"] == "E0001"


def test_plan_only_refuses_commands_and_never_executes_cases(tmp_path):
    marker = tmp_path / "must-not-exist"
    report, _, artifacts = execute(
        tmp_path,
        [
            action(
                "run_command", argv=[sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]
            ),
            plan(),
        ],
        plan_only=True,
    )
    assert report["status"] == "planned"
    assert report["findings"] == []
    assert not marker.exists()
    assert not artifacts.observations[0]["ok"]


def test_source_reads_cannot_establish_a_pass(tmp_path):
    (tmp_path / "README.md").write_text("It works!")
    report, agent, _ = execute(
        tmp_path,
        [
            action("read_file", path="README.md"),
            plan(),
            finding(evidence=["E0001"]),
            {"kind": "finish", "note": "No running application"},
        ],
    )
    assert "actual execution evidence" in agent.prompts[-1]
    assert report["findings"][0]["status"] == "inconclusive"
    assert exit_code(report) == 2


def test_budget_and_questions_preserve_untested_cases(tmp_path):
    report, _, _ = execute(tmp_path, [plan()], max_steps=1)
    assert report["findings"][0]["status"] == "inconclusive"
    report, _, _ = execute(
        tmp_path, [plan(questions=["Which test account?"])], allow_exec=False
    )
    assert report["status"] == "blocked"
    assert report["findings"][0]["status"] == "blocked"


def test_execution_attempts_startup_before_interactively_resolving_setup(tmp_path):
    answers = []

    async def ask_user(prompt):
        answers.append(prompt)
        return "Start the memory-service port forward on localhost:18080."

    artifacts = Artifacts(tmp_path / "runs")
    tools = LocalTools(tmp_path, artifacts, allow_exec=True)
    agent = ScriptedAgent(
        [
            plan(questions=["How is the app started locally?"]),
            action("run_command", argv=[sys.executable, "-c", "import sys; sys.exit(1)"]),
            question(),
            action("run_command", argv=[sys.executable, "-c", "print('ready')"]),
            finding(evidence=["E0002"]),
        ]
    )

    async def run():
        try:
            return await Verification(
                agent, tools, artifacts, progress=lambda _: None, ask_user=ask_user
            ).run("Test greeting")
        finally:
            await tools.close()

    report = asyncio.run(run())
    assert report["status"] == "complete"
    assert answers == ["Do you normally start a local dependency before this app?"]
    assert report["setup_questions"] == answers
    assert "Execution is enabled" in agent.prompts[1]
    assert "Start the memory-service port forward" in agent.prompts[3]


def test_progress_reports_a_short_plan_and_meaningful_milestones(tmp_path):
    messages = []
    artifacts = Artifacts(tmp_path / "runs")
    tools = LocalTools(tmp_path, artifacts, allow_exec=True)
    agent = ScriptedAgent(
        [
            {
                **plan(),
                "plan": {
                    **plan()["plan"],
                    "startup": [
                        "Port-forward the permissions service.",
                        "Launch the app with uvicorn.",
                    ],
                },
            },
            action("start_process", argv=["kubectl", "port-forward", "svc/permissions", "9081:9081"]),
            action("start_process", argv=["curl", "http://localhost:8080/invoke-stream"]),
            action("run_command", argv=[sys.executable, "-c", "print('checked')"]),
            finding(evidence=["E0003"]),
        ]
    )

    async def run():
        try:
            return await Verification(agent, tools, artifacts, progress=messages.append).run(
                "Test greeting"
            )
        finally:
            await tools.close()

    asyncio.run(run())
    assert messages[0] == "Plan:"
    assert "  Setup: forward permissions service" in messages
    assert "  Start app: run DEA under rockstg" in messages
    assert "[1] Setup: start port forward for svc/permissions 9081:9081" in messages
    assert "    $ kubectl port-forward svc/permissions 9081:9081" in messages
    assert "[2] Test: send the application request" in messages
    assert "    $ curl http://localhost:8080/invoke-stream" in messages
    assert not any("asking agent" in message for message in messages)


def test_provider_error_leaves_a_partial_report(tmp_path):
    artifacts = Artifacts(tmp_path)
    tools = LocalTools(tmp_path, artifacts)
    verification = Verification(ScriptedAgent([plan()]), tools, artifacts, progress=lambda _: None)
    with pytest.raises(
        RuntimeError
    ):  # exhausted fixture iterator becomes RuntimeError in coroutine
        asyncio.run(verification.run("Test greeting"))
    report = json.loads((artifacts.path / "report.json").read_text())
    assert report["status"] == "error"
    assert report["findings"][0]["status"] == "inconclusive"


def test_full_acp_subprocess_to_langgraph_to_terminal(tmp_path):
    script = tmp_path / "decisions.json"
    script.write_text(
        json.dumps(
            [
                plan(),
                action("run_command", argv=[sys.executable, "-c", "print('hello')"]),
                finding(evidence=["E0001"]),
            ]
        )
    )
    provider = provider_for(
        "fixture", [sys.executable, str(Path(__file__).with_name("fake_agent.py")), str(script)]
    )
    artifacts = Artifacts(tmp_path / "runs")
    tools = LocalTools(tmp_path, artifacts, allow_exec=True)
    agent = ACPDecisionAgent(provider, artifacts.path, timeout=20)

    async def run():
        try:
            return await Verification(agent, tools, artifacts, progress=lambda _: None).run(
                "Test greeting"
            )
        finally:
            await tools.close()
            await agent.close()

    report = asyncio.run(run())
    assert exit_code(report) == 0
    assert report["evidence_count"] == 1


def test_strict_decision_contract():
    with pytest.raises(ValueError):
        parse_decision('{"kind":"finish", "action":{"tool":"run_command", "reason":"oops"}}')
    assert parse_decision('```json\n{"kind":"finish"}\n```').kind == "finish"


def test_failure_exit_code():
    assert exit_code({"status": "complete", "findings": [{"status": "failed"}]}) == 1


@pytest.mark.parametrize("repeat", [False, True])
def test_native_tool_cancellation_retries_once(tmp_path, repeat):
    from types import SimpleNamespace

    from langgraph_acp import ACPEventType

    class Session:
        calls = 0
        cancellations = 0
        ended = False

        async def cancel(self):
            self.cancellations += 1

        async def prompt(self, prompt):
            self.calls += 1
            if self.calls == 2:
                assert self.ended
                assert "original task context" in prompt
                assert "cancelled" in prompt
            if self.calls == 1 or repeat:
                yield SimpleNamespace(type=ACPEventType.TOOL_STARTED, data={})
                self.ended = True
                yield SimpleNamespace(
                    type=ACPEventType.PROMPT_COMPLETED, data={"stopReason": "cancelled"}
                )
            else:
                yield SimpleNamespace(
                    type=ACPEventType.MESSAGE_DELTA,
                    data={"content": {"type": "text", "text": '{"kind":"finish"}'}},
                )
                yield SimpleNamespace(
                    type=ACPEventType.PROMPT_COMPLETED, data={"stopReason": "end_turn"}
                )

    agent = ACPDecisionAgent(None, tmp_path)
    agent.client = object()
    agent.session = Session()
    if repeat:
        with pytest.raises(RuntimeError, match="repeatedly"):
            asyncio.run(agent.decide("original task context"))
    else:
        assert asyncio.run(agent.decide("original task context")).kind == "finish"
    assert agent.session.calls == 2
    assert agent.session.cancellations == (2 if repeat else 1)


def test_error_summary_prefers_underlying_exception(tmp_path):
    artifacts = Artifacts(tmp_path)
    verification = Verification(None, LocalTools(tmp_path, artifacts), artifacts)
    output = (
        "\x1b[31mUnhandled Errors\x1b[0m\n"
        "Error: Failed to start forks worker\n"
        "\x1b[31mCaused by: TypeError: webidl.util.markAsUncloneable is not a function\x1b[0m"
    )
    assert verification.error_summary({"output": output}) == (
        "Caused by: TypeError: webidl.util.markAsUncloneable is not a function"
    )
