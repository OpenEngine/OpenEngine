"""Deterministic verification lifecycle with pluggable reasoning and execution."""

import asyncio
import contextlib
import re
from collections.abc import Awaitable, Callable
from typing import TypedDict
from urllib.parse import urlsplit

from open_verify.artifacts import Artifacts
from open_verify.backend_runner import BackendRunner
from open_verify.backend_spec import BackendTest
from open_verify.changes import Change, DiffRequest, read_file_diff
from open_verify.engine import Engine
from open_verify.executor import AgentExecutor, DecisionAgent, DecisionContext, StepExecutor
from open_verify.journey import JourneyRunner
from open_verify.journey_spec import RunJourney
from open_verify.manifest import write_manifest
from open_verify.models import Case, Decision, Finding
from open_verify.procedures import Procedures
from open_verify.step_executor import AgentJourneyExecutor, JourneyExecutor
from open_verify.test_spec import BrowserRunner, BrowserTest, LoginRequest
from open_verify.verification import CaseVerifier


class QAState(TypedDict, total=False):
    request: str
    project: str
    stage: str
    steps: int
    status: str
    plan: dict | None
    findings: list[dict]
    observation: dict | None
    decision: dict
    feedback: str
    note: str
    setup_questions: list[str]
    impact: dict | None


class VerificationRunner:
    """Own the verification lifecycle; executors propose and engines perform."""

    def __init__(
        self,
        agent: DecisionAgent | None,
        tools: Engine,
        artifacts: Artifacts,
        *,
        plan_only=False,
        max_steps=60,
        progress=print,
        progress_status=lambda _: None,
        ask_user: Callable[[str], Awaitable[str | None]] | None = None,
        change: Change | None = None,
        test_runner: BrowserRunner | None = None,
        interactive_login=False,
        setup_files=(),
        max_cases=1,
        executor: StepExecutor | None = None,
        journey_executor: JourneyExecutor | None = None,
        replay_cache=None,
        cache_mode="auto",
    ):
        self.executor = executor or (AgentExecutor(agent) if agent is not None else None)
        self.engine = tools
        self.artifacts = artifacts
        self.plan_only = plan_only
        self.max_steps = max_steps
        self.progress = progress
        self.progress_status = progress_status
        self.ask_user = ask_user
        self.change = change
        self.test_runner = test_runner
        self.verifier = CaseVerifier(
            artifacts, test_runner, change_mode=change is not None,
            check_url=tools.check_url, progress=progress,
        )
        self.test_results = self.verifier.test_results
        self.backends = BackendRunner(tools, artifacts, progress=progress)
        self.interactive_login = interactive_login
        self.setup_files = tuple(setup_files)
        self.max_cases = max_cases
        if test_runner is not None:
            test_runner.progress = progress
        self.authentication = tools.create_authentication(progress=progress)
        if test_runner is not None:
            test_runner.authentication = self.authentication
        if journey_executor is None and agent is not None and hasattr(agent, "respond"):
            journey_executor = AgentJourneyExecutor(agent)
        self.journeys = JourneyRunner(tools, artifacts, journey_executor,
                                     authentication=self.authentication, progress=progress,
                                     replay_cache=replay_cache, cache_mode=cache_mode)
        self.procedures = Procedures()
        self.state: QAState = {}
        self._discovery_announced = False
        self._cleanup_announced = False
        self._execution_step = 0
        self._process_progress: dict[str, str] = {}
        self._local_app_process_id: str | None = None
        self._active_case: str | None = None
        self._setup_answers: list[str] = []

    async def decide(self, state: QAState):
        if state["steps"] >= self.max_steps:
            return {"status": "incomplete", "note": "Action/decision budget exhausted."}
        if state["stage"] == "discover":
            waiting_status = "Discovering: asking the QA agent which project file to inspect…"
            self.progress_status(waiting_status)
            self._discovery_announced = True
        elif state["stage"] == "execute":
            waiting_status = "Working: asking the QA agent for the next setup or test step…"
            self.progress_status(waiting_status)
        else:
            waiting_status = "Working: asking the QA agent for the next step…"
        node = self.procedures.locate(state["stage"], state.get("observation"))
        case_id = None
        case = None
        if state["stage"] == "execute" and state.get("plan"):
            completed = {item["case_id"] for item in state["findings"]}
            case = next((c for c in state["plan"]["cases"] if c["id"] not in completed), None)
            if case is not None:
                case_id = case["id"]
                if self._active_case != case_id:
                    self._active_case = case_id
                    self.progress(f"Case: {case_id} — starting case execution")
        context = {
            "state": state,
            "case_instruction": "Complete only the current case. Reuse managed setup; do not stop shared services between cases.",
            "setup_answers": self._setup_answers[-5:],
            "authenticated_session_available": self.authentication.state is not None,
            "recent_evidence": self.artifacts.observations[-6:],
            **self.engine.environment(),
            "execution_enabled": self.engine.allow_exec,
            "max_cases": self.max_cases,
            "setup_files": list(self.setup_files),
            "tools": self.engine.catalog(state["stage"]),
            "procedure_node": node,
            "procedural_guidance": self.procedures.guidance(node),
            "evidence_index": [
                {"id": item["id"], "tool": item["tool"], "ok": item["ok"]}
                for item in self.artifacts.observations
            ],
            "decision_schema": Decision.model_json_schema(),
        }
        if self.change is not None:
            context["tools"]["read_change_diff"] = {
                "description": "Read a paginated patch for one changed file, including hunks omitted from the initial preview.",
                "arguments": DiffRequest.model_json_schema(),
            }
            context["change"] = self.change.model_dump(
                exclude=set() if state["steps"] == 0 else {"diff"}
            )
            if state["stage"] == "execute":
                context["tools"]["run_browser_test"] = {
                    "description": "Generate and execute an isolated Playwright regression test.",
                    "arguments": BrowserTest.model_json_schema(),
                }
                context["tools"]["assisted_login"] = {
                    "description": "Click sign-in in a private visible browser and wait for user login/MFA. Requires real OAuth config and interactive terminal.",
                    "arguments": LoginRequest.model_json_schema(),
                }
        if state["stage"] == "execute" and case is not None and case["interface"] in {"http", "terminal"}:
            context["tools"]["run_backend_test"] = {
                "description": "Save and execute this case's HTTP or terminal regression suite once. The host owns its verdict.",
                "arguments": BackendTest.model_json_schema(),
            }
        if case is not None and case.get("journey"):
            context["tools"].pop("run_browser_test", None)
            context["tools"]["run_journey"] = {
                "description": "Execute this case's fixed act/assert journey after setup. The host owns its verdict.",
                "arguments": RunJourney.model_json_schema(),
            }
        request = DecisionContext.snapshot(
            context, scope=f"case:{case_id}" if case_id is not None else "discover",
            case_id=case_id, change_mode=self.change is not None,
        )
        # One live ACP session retains previous file reads and observations. State
        # contains only the latest observation, avoiding repeated full transcripts.
        ticker = asyncio.create_task(self.waiting_status(waiting_status))
        try:
            if self.executor is None:
                raise ValueError("Supply an agent or an executor")
            proposed = await self.executor.decide(request)
            decision = Decision.model_validate(
                proposed.model_dump() if isinstance(proposed, Decision) else proposed
            )
        finally:
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker
        return {"decision": decision.model_dump(), "steps": state["steps"] + 1, "feedback": ""}

    async def waiting_status(self, initial: str) -> None:
        """Keep a truthful one-line indication while the external QA agent is thinking."""
        elapsed = 0
        while True:
            await asyncio.sleep(5)
            elapsed += 5
            self.progress_status(f"{initial[:-1]} ({elapsed}s)…")

    async def apply(self, state: QAState):
        decision = Decision.model_validate(state["decision"])
        if decision.kind == "action":
            action = decision.action
            if state["stage"] == "discover":
                self.progress_status(self.discovery_progress(action.tool, action.arguments))
            message = self.action_progress(action.tool, action.arguments)
            if message:
                self._execution_step += 1
                self.progress(f"[{self._execution_step}] {message}")
                command = self.command_progress(action.tool, action.arguments)
                if command:
                    self.progress(f"    $ {command}")
            if action.tool == "read_change_diff":
                try:
                    if self.change is None:
                        raise ValueError("Patch inspection requires --base")
                    result = await read_file_diff(self.engine.project, self.change,
                                                  DiffRequest.model_validate(action.arguments))
                    observation = self.artifacts.record(action.tool, action.arguments, result, True)
                except Exception as exc:
                    observation = self.artifacts.record(action.tool, action.arguments, {"error": str(exc)}, False)
            elif action.tool == "assisted_login":
                try:
                    if state["stage"] != "execute":
                        raise ValueError("Assisted login is available only after planning")
                    if not self.interactive_login:
                        raise ValueError("Assisted login requires an interactive terminal")
                    request = LoginRequest.model_validate(action.arguments)
                    self.engine.check_url(request.url)
                    result = await self.authentication.run(
                        request, capture_media=bool((state.get('impact') or {}).get('material_ui_change')),
                    )
                    observation = self.artifacts.record(action.tool, action.arguments, result, True)
                except Exception as exc:
                    observation = self.artifacts.record(action.tool, action.arguments, {"error": str(exc)}, False)
                    self.progress("Login: " + str(exc))
                    if self.ask_user is not None:
                        answer = await self.ask_user("Login did not complete. Retry or skip authenticated cases?")
                        return {"observation": observation, "feedback": "Login response: " + (answer or "skip")}
            elif action.tool in {"run_journey", "run_backend_test"}:
                execute = self.run_journey if action.tool == "run_journey" else self.run_backend_test
                observation = await execute(state, action.arguments)
                if observation["ok"]:
                    result = observation["result"]
                    finding = Finding(case_id=result["case_id"], status=result["status"],
                        actual=result["detail"], evidence=[observation["id"]],
                        reproduction=result["rerun"] if result["status"] == "failed" else [])
                    self.progress(f"Result: {finding.case_id} {finding.status} — {finding.actual}")
                    findings = [*state["findings"], finding.model_dump()]
                    return {"observation": observation, "findings": findings,
                        "status": "complete" if len(findings) == len(state["plan"]["cases"]) else "running"}
            elif action.tool == "run_browser_test":
                observation = await self.run_browser_test(state, action.arguments)
                if observation["ok"]:
                    result = observation["result"]
                    attempts = [r for r in self.test_results if r.case_id == result["case_id"]]
                    if result["status"] == "passed" or len(attempts) >= 2:
                        finding = Finding(
                            case_id=result["case_id"], status=result["status"],
                            actual=result["detail"], evidence=[observation["id"]],
                            reproduction=result["rerun"] if result["status"] == "failed" else [],
                        )
                        self.progress(f"Result: {finding.case_id} {finding.status} — {finding.actual}")
                        findings = [*state["findings"], finding.model_dump()]
                        return {"observation": observation, "findings": findings,
                                "status": "complete" if len(findings) == len(state["plan"]["cases"]) else "running"}
            else:
                observation = await self.engine.execute(
                    action.tool, action.arguments, stage=state["stage"]
                )
            if (
                action.tool == "start_process"
                and "uvicorn" in " ".join(action.arguments.get("argv", []))
                and observation["ok"]
            ):
                self._local_app_process_id = observation["result"]["process_id"]
            outcome = self.action_outcome(action.tool, action.arguments, observation)
            if outcome:
                self.progress(f"    {outcome}")
            if self.is_stream_error(action.tool, observation):
                await self.show_local_app_error()
            return {"observation": observation}
        if decision.kind == "impact":
            if self.change is None or state["stage"] != "discover" or state.get("impact"):
                return {"feedback": "Impact assessment is available once, before a change's plan."}
            impact = decision.impact
            if any(path not in self.change.files for path in impact.affected_files):
                return {"feedback": "Impact cites a file outside the inspected change."}
            if self.change.files and not impact.affected_files:
                return {"feedback": "Cite the changed files supporting the impact assessment."}
            if impact.decision == "skip" and (self.change.truncated or self.change.excluded_files):
                return {"feedback": "Change inspection is incomplete; verify or report uncertain."}
            self.artifacts.write("impact.json", impact.model_dump())
            self.progress(f"Impact: {impact.decision} — {impact.reason}")
            update = {"impact": impact.model_dump()}
            if impact.decision != "verify":
                update.update(status="skipped" if impact.decision == "skip" else "blocked",
                              note=impact.reason)
            return update
        if decision.kind == "plan":
            if state["stage"] != "discover":
                return {"feedback": "The plan is fixed for this run. Assess its existing cases."}
            if self.change is not None and not state.get("impact"):
                return {"feedback": "Assess the change's impact before creating a plan."}
            plan = decision.plan.model_dump()
            if len(plan["cases"]) > self.max_cases:
                return {"feedback": f"Plan exceeds max_cases={self.max_cases}. Combine the requested checks into complete journeys and omit unrequested scenarios."}
            for case in plan["cases"]:
                if case.get("journey"):
                    if case["interface"] != "browser":
                        return {"feedback": "Structured journeys currently require the browser interface."}
                    assertions = [s["instruction"] for s in case["journey"]["steps"] if s["kind"] == "assert"]
                    if len(set(assertions)) != len(assertions):
                        return {"feedback": "Each journey assertion needs a unique instruction."}
                    if case["checks"] and set(case["checks"]) != set(assertions):
                        return {"feedback": "Journey assertion instructions must cover the case checks verbatim."}
                    case["checks"] = assertions
                else:
                    case["checks"] = case["checks"] or [case["expected"]]
                if any(not check.strip() for check in case["checks"]) or len(set(case["checks"])) != len(case["checks"]):
                    return {"feedback": "Completion checks must be nonempty and unique."}
            if (state.get("impact") or {}).get("material_ui_change") and not any(
                case["interface"] in {"browser", "mixed"} for case in plan["cases"]
            ):
                return {"feedback": "A material UI change needs a browser journey in its plan."}
            self.artifacts.write("plan.json", plan)
            self.show_plan(plan)
            if self.plan_only:
                return {"plan": plan, "status": "planned", "note": "Plan only; no cases executed."}
            if plan["questions"] and not self.engine.allow_exec:
                return {
                    "plan": plan,
                    "status": "blocked",
                    "note": "Answer the plan's questions in a new request before execution.",
                }
            if plan["questions"]:
                return {
                    "plan": plan,
                    "stage": "execute",
                    "observation": None,
                    "feedback": (
                        "Execution is enabled. Treat these plan questions as unresolved setup "
                        "assumptions and first attempt documented or likely local startup. Ask a "
                        "targeted question only after observing a startup or readiness blocker."
                    ),
                }
            return {"plan": plan, "stage": "execute", "observation": None}
        if decision.kind == "finding":
            finding = decision.finding
            error = self.validate_finding(state, finding)
            if error:
                return {"feedback": error}
            self.progress(
                f"Result: {finding.case_id} {finding.status} — {self.short_text(finding.actual)}"
            )
            findings = [*state["findings"], finding.model_dump()]
            return {
                "findings": findings,
                "status": "complete" if len(findings) == len(state["plan"]["cases"]) else "running",
            }
        if decision.kind == "question":
            question = decision.question
            error = self.validate_setup_question(state, question.evidence[0])
            if error:
                return {"feedback": error}
            self.progress(f"  Setup question: {question.text}")
            questions = [*state.get("setup_questions", []), question.text]
            if self.ask_user is None:
                return {"setup_questions": questions, "status": "blocked", "note": question.text}
            answer = await self.ask_user(question.text)
            if answer is None or not answer.strip():
                return {"setup_questions": questions, "status": "blocked", "note": question.text}
            self._setup_answers.append(answer.strip()[:4000])
            return {
                "setup_questions": questions,
                "feedback": (
                    "The user answered the setup question: "
                    + answer.strip()
                    + " Continue execution; do not repeat the question."
                ),
            }
        return {
            "status": "incomplete",
            "note": decision.note or "Agent stopped before assessing all cases.",
        }

    def validate_finding(self, state: QAState, finding: Finding) -> str | None:
        """Validate a proposal against host evidence and the current case."""
        return self.verifier.validate_finding(state, finding, active_case=self._active_case)

    async def run_browser_test(self, state: QAState, arguments: dict) -> dict:
        """Execute a typed journey under the independent verification policy."""
        return await self.verifier.run_browser_test(state, arguments, active_case=self._active_case)

    async def run_backend_test(self, state: QAState, arguments: dict) -> dict:
        """Fix case coverage and execute one generated backend suite without automatic retry."""
        try:
            test = BackendTest.model_validate(arguments)
            if state["stage"] != "execute" or not state.get("plan"):
                raise ValueError("Create a plan before executing a backend test")
            completed = {item["case_id"] for item in state["findings"]}
            current = next((c for c in state["plan"]["cases"] if c["id"] not in completed), None)
            if current is None or current["id"] != test.case_id or current["interface"] != test.interface:
                raise ValueError("Execute only the current HTTP/terminal case with its matching interface")
            if current.get("journey") or set(test.checks) != set(current["checks"]):
                raise ValueError("Test must cover every planned completion check verbatim")
            if any(r.case_id == test.case_id for r in self.test_results):
                raise ValueError("This backend case already ran; automatic retries are not allowed")
            slot = len(self.test_results)

            def checkpoint(result):
                if len(self.test_results) == slot:
                    self.test_results.append(result)
                else:
                    self.test_results[slot] = result

            result = await self.backends.run(test, on_result=checkpoint)
            checkpoint(result)
            self.progress(f"  Artifact: {self.artifacts.path / result.test_file}")
            return self.artifacts.record("run_backend_test", arguments, result.model_dump(), True)
        except Exception as exc:
            return self.artifacts.record("run_backend_test", arguments, {"error": str(exc)}, False)

    async def run_journey(self, state: QAState, arguments: dict) -> dict:
        """Execute only the fixed current case; checkpoint evidence before media work."""
        try:
            request = RunJourney.model_validate(arguments)
            if state["stage"] != "execute" or not state.get("plan"):
                raise ValueError("Create a plan before executing a journey")
            completed = {item["case_id"] for item in state["findings"]}
            current = next((c for c in state["plan"]["cases"] if c["id"] not in completed), None)
            if current is None or current["id"] != request.case_id or not current.get("journey"):
                raise ValueError("Execute only the current case's structured journey")
            if any(r.case_id == request.case_id for r in self.test_results):
                raise ValueError("This journey already ran; its checks cannot be revised")
            slot = len(self.test_results)

            def checkpoint(result):
                if len(self.test_results) == slot:
                    self.test_results.append(result)
                else:
                    self.test_results[slot] = result

            result = await self.journeys.run(Case.model_validate(current),
                capture_media=bool((state.get("impact") or {}).get("material_ui_change")),
                on_result=checkpoint)
            checkpoint(result)
            summaries = [p for p in result.screenshots if p.endswith(".gif")]
            for relative in [result.test_file, *(summaries[-1:] or result.screenshots[-1:])]:
                self.progress(f"  Artifact: {self.artifacts.path / relative}")
            for omission in result.omissions:
                self.progress(f"  {omission}")
            return self.artifacts.record("run_journey", arguments, result.model_dump(), True)
        except Exception as exc:
            return self.artifacts.record("run_journey", arguments, {"error": str(exc)}, False)

    def show_plan(self, plan: dict) -> None:
        self.progress("Plan:")
        shown = set()
        for step in plan["startup"][:5]:
            summary = self.plan_step_summary(step)
            if summary not in shown:
                self.progress(f"  {summary}")
                shown.add(summary)
        for case in plan["cases"]:
            self.progress(f"  Test: {case['id']} — {self.short_text(case['title'])}")

    def action_progress(self, tool: str, arguments: dict) -> str | None:
        if tool == "start_process":
            argv = arguments.get("argv", [])
            command = " ".join(argv)
            if "port-forward" in command:
                target = command.split("port-forward", 1)[1].strip()
                if "appstg" in command:
                    return f"Setup: select appstg AWS session and port-forward {target}"
                if "genaistg" in command:
                    return f"Setup: select genaistg AWS session and port-forward {target}"
                return f"Setup: start port forward for {target}"
            if "uvicorn" in command:
                return "Start app: launch the local service"
            if "curl" in argv:
                return "Test: send the application request"
            return "Start: launch a managed local process"
        if tool == "run_command":
            argv = arguments.get("argv", [])
            command = " ".join(argv)
            if "kubectl" in command or "aws" in command:
                return "Setup: verify staging access"
            return "Setup: check local prerequisites"
        if tool == "http_request":
            url = arguments.get("url", "")
            path = urlsplit(url).path
            if path in {"/healthz", "/readyz"}:
                return "Start app: check readiness"
            return f"Test: send {arguments.get('method', 'GET')} {path or url}"
        if tool == "wait":
            return f"Setup: wait {arguments.get('seconds', 60):g} seconds for AWS session propagation"
        if tool == "stop_process" and not self._cleanup_announced:
            self._cleanup_announced = True
            return "Cleanup: stop processes started for this run"
        return None

    def discovery_progress(self, tool: str, arguments: dict) -> str:
        """Describe the current discovery operation without emitting a permanent log line."""
        path = self.short_text(str(arguments.get("path", "")), 90)
        if tool == "read_file" and path:
            return f"Discovering: reading {path}…"
        if tool == "read_change_diff" and path:
            return f"Discovering: inspecting changes in {path}…"
        if tool == "list_files":
            return f"Discovering: scanning {path or 'the project'}…"
        return "Discovering: checking the local startup and test contract…"

    def command_progress(self, tool: str, arguments: dict) -> str | None:
        if tool in {"run_command", "start_process"}:
            command = " ".join(arguments.get("argv", []))
            # A live API test is most useful when its exact curl invocation is visible.
            if "curl" in arguments.get("argv", []):
                return command
            return self.short_text(command, 180)
        if tool == "http_request":
            return f"{arguments.get('method', 'GET')} {arguments.get('url', '')}"
        return None

    def action_outcome(self, tool: str, arguments: dict, observation: dict) -> str | None:
        result = observation.get("result", {})
        if tool == "run_command":
            command = " ".join(arguments.get("argv", []))
            if "lsof" in command and result.get("exit_code") not in {0, None}:
                return "• not running yet; starting it"
            if not observation["ok"] or result.get("exit_code") not in {0, None}:
                return "✗ setup command failed: " + self.error_summary(result)
            return "✓ completed"
        if tool == "start_process":
            if not observation["ok"]:
                return "✗ could not start: " + self.error_summary(result)
            if "curl" in result.get("argv", []):
                return "… request started; waiting for its response"
            return "… started; waiting for readiness"
        if tool == "http_request":
            status = result.get("status")
            if not observation["ok"]:
                return "✗ request failed: " + self.error_summary(result)
            mark = "✓" if status is not None and 200 <= status < 400 else "✗"
            return f"{mark} HTTP {status}"
        if tool == "wait":
            return "✓ wait complete; retrying the affected setup step"
        if tool == "process_output":
            process_id = result.get("process_id", observation.get("arguments", {}).get("process_id", "process"))
            output = result.get("output", "")
            exit_code = result.get("exit_code")
            if exit_code is None and "Forwarding from" in output:
                status = "✓ port forward is ready"
            elif exit_code is None and "Uvicorn running" in output:
                status = "✓ local app is ready"
            elif exit_code is None:
                return None
            elif exit_code == 0:
                if "curl" in " ".join(result.get("argv", [])):
                    status = self.stream_request_outcome(output, result.get("log"))
                else:
                    status = "✓ process completed"
            else:
                label = self.process_label(result)
                status = (
                    f"✗ {label} exited ({exit_code})\n"
                    f"      Error: {self.error_summary(result)}"
                )
            if self._process_progress.get(process_id) == status:
                return None
            self._process_progress[process_id] = status
            return status
        if tool == "stop_process" and observation["ok"]:
            return "✓ stopped"
        return None

    def stream_request_outcome(self, output: str, log_name: str | None) -> str:
        """Show an SSE response inline, even though curl itself exits successfully."""
        normalized = output.replace("\r\n", "\n").replace("\r", "\n")
        blocks = re.findall(r"(?ms)^event:.*?(?=^event:|\Z)", normalized)
        events = []
        for block in blocks:
            lines = [
                line for line in block.splitlines() if line.startswith(("event:", "data:"))
            ]
            if lines:
                events.append("\n".join(lines))
        response = "\n\n".join(events) or "No parseable SSE event was returned."
        suffix = ""
        if log_name:
            suffix = f"\n      Full request/response transcript: {self.artifacts.path / log_name}"
        mark = "✗" if any("event: error" in event for event in events) else "✓"
        return f"{mark} Response (SSE):\n      {response.replace(chr(10), chr(10) + '      ')}{suffix}"

    @staticmethod
    def is_stream_error(tool: str, observation: dict) -> bool:
        result = observation.get("result", {})
        return tool == "process_output" and "curl" in result.get("argv", []) and "event: error" in result.get("output", "")

    async def show_local_app_error(self) -> None:
        """Immediately pair a public SSE error with the useful local server log error."""
        if self._local_app_process_id is None:
            return
        observation = await self.engine.execute(
            "process_output", {"process_id": self._local_app_process_id}, stage="execute"
        )
        if not observation["ok"]:
            return
        summary = self.runtime_error_summary(observation["result"].get("output", ""))
        if summary:
            self.progress(f"      ✗ DEA log error: {summary}")

    def runtime_error_summary(self, output: str) -> str | None:
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        for marker in (
            "nodename nor servname",
            "gaierror",
            "connecterror",
            "connection refused",
            "permission denied",
            "authentication failed",
        ):
            match = next((line for line in lines if marker in line.lower()), None)
            if match:
                return self.short_text(match, 240)
        return None

    @staticmethod
    def plan_step_summary(step: str) -> str:
        text = step.lower()
        if "permissions" in text and "port-forward" in text:
            return "Setup: forward permissions service"
        if ("conversation-history" in text or "memory" in text) and "port-forward" in text:
            return "Setup: forward conversation history service"
        if "rockstg" in text or "uvicorn" in text:
            return "Start app: run DEA under rockstg"
        if "/healthz" in text or "/readyz" in text or "readiness" in text:
            return "Start app: confirm health and readiness"
        if "environment" in text or ".env" in text:
            return "Setup: use local application configuration"
        return "Setup: inspect local prerequisites"

    @staticmethod
    def process_label(result: dict) -> str:
        command = " ".join(result.get("argv", []))
        if "port-forward" in command:
            target = command.split("port-forward", 1)[1].strip()
            return f"port forward for {target}"
        if "uvicorn" in command:
            return "local app"
        if "curl" in command:
            return "application request"
        return "process"

    def error_summary(self, result: dict) -> str:
        text = result.get("error") or result.get("output") or "no diagnostic output"
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for marker in (
            "caused by:",
            "typeerror:",
            "referenceerror:",
            "no such host",
            "unable to connect",
            "expired",
            "invalid",
            "denied",
            "refused",
            "timed out",
            "error",
            "failed",
        ):
            match = next((line for line in lines if marker in line.lower()), None)
            if match:
                return self.short_text(match, 200)
        for line in lines:
            if not line.startswith("["):
                return self.short_text(line, 200)
        return "no diagnostic output"

    @staticmethod
    def short_text(text: str, limit: int = 120) -> str:
        normalized = " ".join(text.split())
        return normalized if len(normalized) <= limit else normalized[: limit - 1] + "…"

    def validate_setup_question(self, state: QAState, evidence_id: str) -> str | None:
        if state.get("stage") != "execute":
            return "Ask setup questions only during execution. Continue discovery or create the plan."
        startup_tools = {"run_command", "start_process", "process_output", "http_request"}
        evidence = {item["id"]: item for item in self.artifacts.observations}
        item = evidence.get(evidence_id)
        if item is None:
            return "The setup question cites unknown evidence. Use one failed startup observation."
        result = item.get("result", {})
        if item["tool"] in startup_tools and (
            not item["ok"] or result.get("timed_out") or result.get("exit_code") not in {None, 0}
        ):
            return None
        return (
            "Ask a setup question only after one cited failed startup or readiness observation. "
            "First attempt a documented or likely safe local startup command and inspect its output."
        )

    def finish(self, state: dict) -> dict:
        # Omitted cases are always visible, including after cancellation or errors.
        if state.get("plan") and state["status"] != "planned":
            seen = {item["case_id"] for item in state["findings"]}
            for case in state["plan"]["cases"]:
                if case["id"] not in seen:
                    state["findings"].append(
                        Finding(
                            case_id=case["id"],
                            status="blocked" if state["status"] == "blocked" else "inconclusive",
                            actual=state.get("note") or "Case was not assessed.",
                        ).model_dump()
                    )
        state.pop("decision", None)
        state.pop("observation", None)
        state["evidence_count"] = len(self.artifacts.observations)
        state["procedure_version"] = self.procedures.graph["version"]
        self.artifacts.report(state)
        self.publish(state)
        return state

    def publish(self, report: dict, *, cleanup_errors=()):
        return write_manifest(
            self.artifacts.path, report, self.change, self.test_results,
            cleanup_errors=cleanup_errors,
        )

    async def run(self, request: str) -> dict:
        initial: QAState = {
            "request": request,
            "project": str(self.engine.project),
            "stage": "discover",
            "steps": 0,
            "status": "running",
            "plan": None,
            "findings": [],
            "note": "",
            "setup_questions": [],
            "impact": None,
        }
        self.state = initial
        try:
            while self.state["status"] == "running":
                self.state.update(await self.decide(self.state))
                if self.state["status"] != "running":
                    break
                self.state.update(await self.apply(self.state))
            result = self.state
        except BaseException as exc:
            status = (
                "interrupted"
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt))
                else "error"
            )
            state = {**self.state, "status": status, "note": f"{type(exc).__name__}: {exc}"}
            self.finish(state)
            raise
        return self.finish(dict(result))


def exit_code(report: dict) -> int:
    if report["status"] in {"planned", "skipped"}:
        return 0
    if any(item["status"] == "failed" for item in report["findings"]):
        return 1
    if report["status"] != "complete" or any(
        item["status"] != "passed" for item in report["findings"]
    ):
        return 2
    return 0
