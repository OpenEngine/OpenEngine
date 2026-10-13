"""Optional forge approval after impact analysis, independent of human acceptance."""
import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import logging
from typing import Any

from engine.domain import ForgeMode, WorkState, WorkspaceId, forge_mode
from engine.graph_runtime.events import EventKind
from engine.graph_runtime_langgraph.executions import current_execution
from engine.runtime.change_requests import change_request

from .ci_check import _PASSING, _TERMINAL


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CaptureReviewedCommit:
    """Pin each review pass to the checkout, never to a later forge head."""

    graph_node_show_in_sidebar: bool = False
    graph_node_name: str = "Record reviewed commit"
    graph_node_kind: str = "tool"
    graph_node_group: str = WorkState.REVIEW

    async def __call__(self, state: Mapping[str, Any]) -> dict[str, object]:
        sha = ""
        workspace = state.get("workspace")
        if workspace:
            process = await asyncio.create_subprocess_exec(
                "git", "-C", str(workspace), "rev-parse", "--verify", "HEAD",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await process.communicate()
            if process.returncode == 0:
                sha = stdout.decode().strip()
        return {"reviewed_sha": sha}


@dataclass(frozen=True)
class AutoApprove:
    graph_node_name: str = "Auto-approve"
    graph_node_kind: str = "tool"
    graph_node_group: str = WorkState.REVIEW
    graph_node_description: str = "Approves Green pull requests when daemon policy and safety checks allow."

    async def __call__(self, state: Mapping[str, Any]) -> dict[str, object]:
        execution = current_execution()
        runtime = execution.runtime
        inputs = state.get("inputs") or {}
        url = str(state.get("pr_url") or inputs.get("pr_url") or "")
        repository = str(state.get("repository", ""))

        async def result(reason: str, *, approved: bool = False) -> dict[str, object]:
            summary = f"Auto-approval {'approved' if approved else 'skipped'}: {reason}"
            payload = {"pr_url": url, "approved": approved, "reason": reason}
            logger.info("%s (%s)", summary, url)
            await execution.emit(
                EventKind.PULL_REQUEST_AUTO_APPROVED if approved else EventKind.PULL_REQUEST_APPROVAL_SKIPPED,
                payload,
            )
            await execution.say(summary)
            return {"auto_approve": {**payload, "summary": summary}}

        if forge_mode(inputs) is ForgeMode.DISCONNECTED:
            return await result("disconnected mode")
        if runtime.repository_mode and await runtime.repository_mode(repository) == "disconnected":
            return await result("disconnected repository")
        policy = runtime.auto_approve_green
        if policy is None or not policy(repository):
            return await result("disabled by daemon repository policy")
        if state.get("impact_level") != "Green":
            return await result("impact analysis did not rate the change Green")
        rationale = state.get("impact_rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            return await result("missing impact rationale")
        if state.get("review"):
            return await result("unresolved review findings")
        if state.get("ci_check", {}).get("passed") is False:
            return await result("CI failed")
        target = change_request(url)
        if target is None:
            return await result("no pull request URL")
        source = runtime.source_control
        workspace = state.get("workspaceId")
        if source is None or not workspace:
            return await result("source control or workspace unavailable")
        reviewed_sha = state.get("reviewed_sha")
        if not isinstance(reviewed_sha, str) or not reviewed_sha:
            return await result("reviewed commit unavailable")
        try:
            # Verify the workspace addresses this PR before checking its CI.
            change = await source.view_change_request(WorkspaceId(str(workspace)), target.number)
            if change_request(change.url) != target or change.state.lower() not in {"open", "opened"}:
                return await result("pull request is not open in the run's repository")
            if change.head_sha != reviewed_sha:
                return await result("pull request head changed since review")
            if any(comment.thread_id and comment.is_resolved is False for comment in change.comments):
                return await result("unresolved review threads")
            # Review-only runs have no preceding CICheck; always read current CI.
            status = await source.list_pipeline_status(
                WorkspaceId(str(workspace)), change_request_number=target.number,
            )
            if status.ref != reviewed_sha:
                return await result("CI commit does not match reviewed commit")
            jobs = (*status.checks, *status.pipelines, *(status.required_checks or ()))
            if (status.required_checks is None and not jobs) or any(
                job.status.lower() not in _TERMINAL
                or (job.conclusion or job.status).lower() not in _PASSING
                for job in jobs
            ):
                return await result("CI failed, pending, or unavailable")
            body = (
                "Approved automatically because impact analysis rated this change Green.\n\n"
                + rationale.strip()
            )
            if not await source.approve_change_request(target.project, target.number, body, commit_sha=reviewed_sha):
                return await result("cannot approve own pull request")
        except Exception as error:
            logger.exception("Could not auto-approve %s", url)
            return await result(f"approval unavailable: {error}")
        return await result(body, approved=True)
