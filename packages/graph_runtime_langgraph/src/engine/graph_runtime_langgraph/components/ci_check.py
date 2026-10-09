"""Wait for a pull request's CI and return a deterministic verdict."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

from engine.domain import ForgeMode, WorkState, WorkspaceId, forge_mode
from engine.graph_runtime_langgraph.executions import current_execution
from engine.ports import SourceControl
from engine.runtime.change_requests import change_request


_TERMINAL = {
    "completed", "success", "failed", "failure", "error", "canceled",
    "cancelled", "skipped", "neutral", "timed_out", "action_required",
    "startup_failure", "stale",
}
_PASSING = {"success", "skipped", "neutral"}


@dataclass(frozen=True, slots=True, kw_only=True)
class CICheck:
    """Poll the PR's current revision until all reported CI has settled.

    Reads ``pr_url`` and ``workspaceId`` from upstream nodes. Required gates
    must all settle; a confirmed absence of requirements passes immediately.
    Providers without requirement discovery retain polling of reported CI. Provider errors and a
    timeout fail the node, rather than claiming CI passed or asking an agent
    to fix an infrastructure error. Cancellation propagates through polling.
    """

    source_control: SourceControl | None = None
    poll_interval: float = 15
    timeout: float = 3600
    output_key: str = "ci_check"
    graph_node_name: str = "CI check"
    graph_node_kind: str = "tool"
    graph_node_description: str = "Waits for CI and returns failures to implementation."
    graph_node_show_in_sidebar: bool = False
    graph_node_group: str = WorkState.IMPLEMENTATION

    async def __call__(self, state: Mapping[str, object]) -> dict[str, object]:
        execution = current_execution()
        if forge_mode(state.get("inputs")) is ForgeMode.DISCONNECTED:
            # There is no pull request to run CI on. Passed, so the graph goes
            # on to review, and said to be skipped, so nobody reads it as green.
            summary = "CI skipped: this run is disconnected from the forge."
            await execution.say(summary)
            return {self.output_key: {"passed": True, "skipped": True, "summary": summary}}
        source_control = self.source_control or execution.runtime.source_control
        if source_control is None:
            raise RuntimeError("CICheck needs a SourceControl bound to its runtime")
        workspace = state.get("workspaceId")
        if not isinstance(workspace, str) or not workspace.strip():
            raise ValueError("CICheck needs workspaceId from an upstream WorkspaceNode")
        url = state.get("pr_url")
        # Read the way the recorders and adapters read it, so the change
        # request CI waits on is the one the rest of the run is bound to.
        found = change_request(url) if isinstance(url, str) else None
        if found is None:
            raise ValueError("CICheck needs a pull request URL in pr_url")
        number = found.number
        await execution.say(f"Waiting for CI on {url}.")
        async with asyncio.timeout(self.timeout):
            while True:
                status = await source_control.list_pipeline_status(
                    WorkspaceId(workspace), change_request_number=number,
                )
                known_requirements = status.required_checks is not None
                jobs = (
                    status.required_checks if known_requirements
                    else (*status.checks, *status.pipelines)
                )
                # GitHub supplies a conclusion; GitLab uses terminal statuses.
                if (known_requirements or jobs) and all(
                    job.status.lower() in _TERMINAL for job in jobs  # pyright: ignore[reportOptionalIterable]  # Baseline: see docs/pyright.md
                ):
                    failed = [
                        job for job in jobs  # pyright: ignore[reportOptionalIterable]  # Baseline: see docs/pyright.md
                        if (job.conclusion or job.status).lower()
                        not in _PASSING
                    ]
                    details = "\n".join(
                        f"- {job.name}: {job.conclusion or job.status} "
                        f"{getattr(job, 'details_url', getattr(job, 'url', ''))}"
                        for job in failed
                    )
                    summary = (
                        f"CI failed for {url} at {status.ref}:\n{details}"
                        if failed else f"CI passed for {url} at {status.ref}."
                    )
                    await execution.say(summary)
                    return {self.output_key: {
                        "passed": not failed, "pr_url": url,
                        "ref": status.ref, "summary": summary,
                    }}
                await asyncio.sleep(self.poll_interval)


__all__ = ["CICheck"]
