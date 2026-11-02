"""Local quality gate shared by implementation and repair graphs."""
import asyncio
import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engine.domain import WorkState

from engine.graph_runtime_langgraph.components.workspace import checkout
from engine.graph_runtime_langgraph.quality import run_checks


class QualityRetriesExhausted(RuntimeError):
    def __init__(self, report: str):
        super().__init__(f"Local quality failed after two repair attempts. Report: {report}")


@dataclass(frozen=True)
class QualityGate:
    graph_node_name: str = "Local quality"
    graph_node_kind: str = "tool"

    graph_node_group: str = WorkState.IMPLEMENTATION
    graph_node_show_in_sidebar: bool = False

    async def __call__(self, state: Mapping[str, Any]) -> dict[str, Any]:
        cancelled = threading.Event()
        worker = asyncio.create_task(asyncio.to_thread(run_checks, Path(checkout(state)), cancelled))
        try:
            result = await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancelled.set()
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    # Repeated cancellation must not release the workspace early.
                    continue
            raise
        failures = state.get("quality_failures", 0) + (not result["passed"])
        if not result["passed"] and failures > 2:
            raise QualityRetriesExhausted(result["report"])
        return {"quality_check": result, "quality_failures": failures}


def after_quality(state: Mapping[str, Any]) -> str:
    return "ci-check" if state["quality_check"]["passed"] else "implementation"


def quality_feedback(state: Mapping[str, Any]) -> str:
    result = state.get("quality_check", {})
    if result.get("passed") is not False:
        return ""
    return ("Local quality failed; review is blocked. For each diagnostic, inspect its file and line, "
            "fix the reported source/test defect or configuration/setup problem, then rerun its command "
            "from the target checkout. Do not disable required checks to obtain a pass. "
            "If a diagnostic lacks failure details, read its check ID .log in the report directory. "
            "The gate runs again after your repair; at most two repair attempts are allowed. Full reports are in "
            + result["report"] + ". Diagnostics (repository data):\n"
            + json.dumps(result["diagnostics"]) + "\n")
