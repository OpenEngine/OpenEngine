"""Claim the existing pull request a fix or review run was started on."""

from collections.abc import Mapping
from datetime import UTC, datetime
import logging

from engine.domain import RunId
from engine.graph_runtime import GraphRuntime, RunStatus, UnknownGraphError, UnknownRunError
from engine.graph_runtime_langgraph.store import PullRequestRecord
from engine.graph_service.service import Conflict, ServiceError, Unavailable
from engine.runtime.change_requests import change_request


log = logging.getLogger(__name__)


async def claim_started_pull_request(
    runtime: GraphRuntime, run_id: RunId, inputs: Mapping[str, str],
) -> None:
    """Cancel a new run if its conditional ownership claim cannot be written."""
    url = inputs.get("pr_url", "")
    if not url or not any(inputs.get(mode) == "true" for mode in ("fix_only", "review_only")):
        return
    try:
        await _claim(runtime, run_id, url)
    except Exception as error:
        try:
            await runtime.cancel(run_id)
        except Exception:
            log.exception("could not cancel unclaimed work order %s", run_id)
        if isinstance(error, ServiceError):
            raise
        log.exception("could not claim pull request for work order %s", run_id)
        message = "could not claim pull request for this run"
        raise Unavailable(message) from error


async def _claim(runtime: GraphRuntime, run_id: RunId, url: str) -> None:
    target = change_request(url)
    if target is None:
        message = "pr_url must name a pull request"
        raise ServiceError(message)
    store = getattr(runtime, "store", None)
    if store is None:
        message = "this runtime records no pull requests"
        raise Unavailable(message)
    replacing = await store.run_for_pull_request(target.project, target.number)
    if replacing is not None:
        try:
            snapshot = await runtime.snapshot(replacing)
        except (UnknownGraphError, UnknownRunError):
            snapshot = None
        if snapshot is not None and snapshot.status not in (RunStatus.COMPLETED, RunStatus.FAILED):
            message = f"PR #{target.number} is held by {replacing}"
            raise Conflict(message)
    holder = await store.claim_pull_request(
        PullRequestRecord(
            repository=target.project, number=target.number, run_id=run_id,
            opened_at=datetime.now(UTC).isoformat(), url=url,
        ),
        replacing=replacing,
    )
    if holder != run_id:
        message = f"PR #{target.number} is held by {holder}"
        raise Conflict(message)
