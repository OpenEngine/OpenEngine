"""Wait for a pull request's CI and return a deterministic verdict."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass, field

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


@dataclass(eq=False, slots=True)
class _Waiter:
    """One `CICheck` waiting on one pull request's CI."""

    repository: str
    number: int
    #: The head the latest status read was for; ``None`` until the first read,
    #: so a delivery that lands during it is not missed for want of a SHA.
    ref: str | None = None
    woken: asyncio.Event = field(default_factory=asyncio.Event)


#: Every `CICheck` waiting right now. In memory, and only as long as each one
#: waits, so it is bounded by the runs at their CI step; a restart forgets it
#: and the fallback poll covers what it would have caught.
_WAITERS: set[_Waiter] = set()


@contextlib.contextmanager
def _waiting(repository: str, number: int) -> Iterator[_Waiter]:
    waiter = _Waiter(repository.lower(), number)
    _WAITERS.add(waiter)
    try:
        yield waiter
    finally:
        _WAITERS.discard(waiter)


def wake_ci(repository: str, sha: str, pull_requests: Collection[int] = ()) -> int:
    """Wake every `CICheck` `sha` may concern in `repository`; how many it woke.

    That is a waiter on `sha` itself, or on one of `pull_requests`, the pull
    requests the delivery says `sha` heads: after a push, CI arrives for a
    head the waiter has not read yet. A delivery that names no pull request,
    as a ``status`` never does, may be such a head too, so it wakes every
    waiter in the repository.

    Only a nudge to read CI again: the verdict still comes from
    ``list_pipeline_status``, never from whatever delivery called this.
    """
    repository, sha = repository.lower(), sha.lower()
    woken = 0
    for waiter in _WAITERS:
        if waiter.repository == repository and (
            waiter.ref in (None, sha) or not pull_requests or waiter.number in pull_requests
        ):
            waiter.woken.set()
            woken += 1
    return woken


@dataclass(frozen=True, slots=True, kw_only=True)
class CICheck:
    """Wait for the PR's current revision until all reported CI has settled.

    CI is read once on starting, so CI that settled first is not waited on,
    then again whenever `wake_ci` reports a delivery that may concern the PR, and
    otherwise every ``poll_interval`` seconds: deliveries are not guaranteed,
    and a deployment without a reachable webhook has only the poll.

    Reads ``pr_url`` and ``workspaceId`` from upstream nodes. Required gates
    must all settle; a confirmed absence of requirements passes immediately.
    Providers without requirement discovery retain polling of reported CI. Provider errors and a
    timeout fail the node, rather than claiming CI passed or asking an agent
    to fix an infrastructure error. Cancellation propagates through polling.
    """

    source_control: SourceControl | None = None
    poll_interval: float = 300
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
            with _waiting(found.project, number) as waiter:
                while True:
                    # Cleared before reading, so a delivery during the read
                    # wakes the next wait rather than being lost.
                    waiter.woken.clear()
                    status = await source_control.list_pipeline_status(
                        WorkspaceId(workspace), change_request_number=number,
                    )
                    waiter.ref = status.ref.lower()
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
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(waiter.woken.wait(), self.poll_interval)


__all__ = ["CICheck", "wake_ci"]
