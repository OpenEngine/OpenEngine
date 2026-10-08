"""Evidence-backed findings and generated-journey policy, independent of the executor."""

from open_verify.artifacts import Artifacts
from open_verify.engine import READ_TOOLS
from open_verify.models import Finding
from open_verify.test_spec import BrowserRunner, BrowserTest, TestResult


class CaseVerifier:
    """Own actual test results; model proposals cannot replace execution evidence."""

    def __init__(self, artifacts: Artifacts, test_runner: BrowserRunner | None, *,
                 change_mode: bool, check_url, progress=print, application_interface=None):
        self.artifacts = artifacts
        self.test_runner = test_runner
        self.change_mode = change_mode
        self.application_interface = application_interface
        self.check_url = check_url
        self.progress = progress
        self.test_results: list[TestResult] = []

    def validate_finding(self, state: dict, finding: Finding, *, active_case: str | None) -> str | None:
        if not state.get("plan"):
            return "Create a plan before reporting findings."
        if finding.case_id not in {case["id"] for case in state["plan"]["cases"]}:
            return "Finding refers to an unknown case."
        if finding.case_id in {item["case_id"] for item in state["findings"]}:
            return "This case already has a finding."
        if active_case is not None and finding.case_id != active_case:
            return "Report only the current case before proceeding to the next case."
        evidence = {item["id"]: item for item in self.artifacts.observations}
        if any(item not in evidence for item in finding.evidence):
            return "Finding cites unknown evidence. Use IDs from evidence_index."
        if finding.status in {"passed", "failed"} and not any(
            evidence[item]["ok"]
            and evidence[item]["tool"] not in READ_TOOLS | {"start_process", "stop_process"}
            for item in finding.evidence
        ):
            return "Passed/failed requires actual execution evidence; file reads and process startup are insufficient."
        if finding.status == "failed" and not finding.reproduction:
            return "A failed case needs reproduction steps."
        case = next(case for case in state["plan"]["cases"] if case["id"] == finding.case_id)
        backend = case["interface"] in {"http", "terminal"}
        has_backend_run = any(item["tool"] == "run_backend_test" and item["ok"]
                              and item["result"]["case_id"] == finding.case_id for item in self.artifacts.observations)
        if case.get("journey") or self.change_mode or has_backend_run or self.application_interface is not None:
            tool = "run_backend_test" if backend else ("run_journey" if case.get("journey") else "run_browser_test")
            runs = [item for item in self.artifacts.observations
                    if item["tool"] == tool and item["ok"]
                    and item["result"]["case_id"] == finding.case_id]
            if finding.status in {"passed", "failed"} and (
                not runs or runs[-1]["id"] not in finding.evidence
                or runs[-1]["result"]["status"] != finding.status
            ):
                return "Cite the latest generated test execution and match its actual status."
        return None

    async def run_browser_test(self, state: dict, arguments: dict, *, active_case: str | None) -> dict:
        try:
            if not self.change_mode or state["stage"] != "execute" or self.test_runner is None:
                raise ValueError("Generated tests require a change plan in execution mode")
            test = BrowserTest.model_validate(arguments)
            if active_case is not None and test.case_id != active_case:
                raise ValueError("Execute only the current case")
            cases = {case["id"]: case for case in state["plan"]["cases"]}
            if test.case_id not in cases or cases[test.case_id]["interface"] not in {"browser", "mixed"}:
                raise ValueError("Test must belong to a planned browser/mixed case")
            if test.case_id in {item["case_id"] for item in state["findings"]}:
                raise ValueError("This case already has a finding")
            if cases[test.case_id].get("journey"):
                raise ValueError("Use run_journey for a structured case; its checks are fixed")
            if set(test.checks) != set(cases[test.case_id]["checks"]):
                raise ValueError("Test must cover every planned completion check, mapped verbatim to assertion step indexes")
            if (self.application_interface == 'browser'
                    and cases[test.case_id].get('interaction', 'user') == 'user'):
                actions = [i for i, step in enumerate(test.steps) if step.kind in {'click', 'fill', 'press'}]
                if not actions or not any(i > actions[0] for indexes in test.checks.values() for i in indexes):
                    raise ValueError('Application smoke requires a real UI action followed by a result assertion')
            previous = [r for r in self.test_results if r.case_id == test.case_id]
            if any(r.status == "passed" for r in previous) or len(previous) >= 2:
                raise ValueError("Case is finished; additional browser runs are not allowed")
            if previous and not test.retry_reason.strip():
                raise ValueError("Retry requires a diagnosis and correction in retry_reason")
            self.check_url(test.url)
            self.progress(f"Test: {cases[test.case_id]['title']} (attempt {len(previous) + 1}/2)")
            if previous:
                self.progress(f"  Retry: {test.retry_reason}")
            result_index = len(self.test_results)

            def checkpoint(result: TestResult):
                # Each attempt occupies one slot; media updates replace its
                # checkpoint so cancellation cannot hide completed execution.
                if len(self.test_results) == result_index:
                    self.test_results.append(result)
                else:
                    self.test_results[result_index] = result

            result = await self.test_runner.run(
                test, capture_media=True,
                on_result=checkpoint,
            )
            checkpoint(result)
            self.progress(f"  {test.case_id}: {result.status} — {result.detail}")
            summaries = [p for p in result.screenshots if p.endswith('.gif')]
            visual = summaries[-1:] or result.screenshots[-1:]
            for relative in [result.test_file, *visual]:
                self.progress(f"  Artifact: {self.artifacts.path / relative}")
            for omission in result.omissions:
                self.progress(f"  {omission}")
            return self.artifacts.record("run_browser_test", arguments, result.model_dump(), True)
        except Exception as exc:
            return self.artifacts.record("run_browser_test", arguments, {"error": str(exc)}, False)

