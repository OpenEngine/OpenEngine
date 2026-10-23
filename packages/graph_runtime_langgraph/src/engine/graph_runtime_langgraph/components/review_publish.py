"""Deterministic publication and approval of a completed review."""
from collections.abc import Mapping
from typing import Any

from engine.domain import ForgeMode, WorkspaceId, forge_mode
from engine.graph_runtime_langgraph.components.findings import Finding
from engine.graph_runtime_langgraph.executions import current_execution
from engine.runtime.change_requests import change_request


def enabled(state: Mapping[str, Any], key: str) -> bool:
    return str(state.get('inputs', {}).get(key, 'false')).lower() == 'true'


def target_url(state: Mapping[str, Any]) -> str:
    return str(state.get('pr_url') or state.get('inputs', {}).get('pr_url', ''))


def review_requester(state: Mapping[str, Any]) -> str:
    """The GitHub login that requested this review, or empty for a manual run."""
    return str(state.get('inputs', {}).get('review_requester', '')).strip()


def review_requester_id(state: Mapping[str, Any]) -> int | None:
    """None only means the field was not supplied; a present `0` still fails the id crosscheck."""
    raw = str(state.get('inputs', {}).get('review_requester_id', '')).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def publishing(state: Mapping[str, Any]) -> bool:
    return enabled(state, 'publish') and forge_mode(state.get('inputs')) is ForgeMode.CONNECTED and bool(target_url(state))


class PublishReview:
    graph_node_name = 'Publish review'
    graph_node_kind = 'tool'
    graph_node_description = 'Posts surviving findings and the impact rating to the PR.'

    async def __call__(self, state: Mapping[str, Any]) -> dict[str, object]:
        execution = current_execution()
        source = execution.runtime.source_control
        if not publishing(state) or source is None:
            message = 'Publishing requires a connected PR and source control'
            raise ValueError(message)
        for item in state.get('review', []):
            finding = Finding.from_dict(item)
            await source.add_comment(target_url(state), finding.as_comment(), file=finding.file, line=finding.line)
        # The rationale opens with its own Impact line, so it is posted unchanged.
        await source.add_comment(target_url(state), str(state['impact_rationale']))
        await execution.say('Published review findings and impact summary.')
        return {'review_published': True}


async def approval_reason(state: Mapping[str, Any]) -> str:
    """Empty means eligible. Unknown forge data fails closed with a visible reason.

    A requester who still holds write access at decision time authorizes
    approval on their own; with no requester (a manual or CLI run),
    `auto_approve_green` is the only authority left.
    """
    requester = review_requester(state)
    if not requester and not enabled(state, 'auto_approve_green'):
        return 'auto_approve_green is disabled'
    if forge_mode(state.get('inputs')) is ForgeMode.DISCONNECTED:
        return 'disconnected mode'
    if not enabled(state, 'publish'):
        return 'local review: publishing is disabled'
    target = change_request(target_url(state))
    if target is None:
        return 'no PR URL'
    if state.get('impact_level') != 'Green':
        return 'impact is not Green'
    if state.get('review'):
        return 'findings survived the reranker'
    source = current_execution().runtime.source_control
    if source is None:
        return 'source control is unavailable'
    try:
        if requester:
            # GitHub's own convention: every Bot-type account's login ends this way.
            if requester.endswith('[bot]'):
                return f'requester {requester} is a bot'
            if not await source.can_write_repository(
                target_url(state), requester, user_id=review_requester_id(state)):
                return f'requester {requester} lacks write access'
        workspace = WorkspaceId(str(state['workspaceId']))
        change = await source.view_change_request(workspace, target.number)
        # The workspace-scoped lookup must name the exact PR we will approve.
        if change_request(change.url) != target:
            return 'target PR does not match the workspace repository'
        if requester and change.author and requester.casefold() == change.author.casefold():
            return f'requester {requester} is the PR author'
        reviewed_sha = state.get('reviewed_sha')
        if not reviewed_sha or reviewed_sha != change.head_sha:
            return 'reviewed commit does not match the PR head'
        login = await source.authenticated_login(target_url(state))
        if not login or not change.author:
            return 'approving identity or PR author is unavailable'
        if login.casefold() == change.author.casefold():
            return 'PR author is the approving identity'
        if requester and login.casefold() == requester.casefold():
            return f'requester {requester} is the approving identity'
        status = await source.list_pipeline_status(workspace, change_request_number=target.number)
        if status.ref != reviewed_sha:
            return 'CI commit does not match the reviewed commit'
        allowed = {'success', 'neutral', 'skipped', 'pending', 'queued', 'in_progress',
                   'running', 'created', 'waiting', 'requested', 'preparing', 'scheduled'}
        if any((job.conclusion or job.status).lower() not in allowed
               for job in (*status.checks, *status.pipelines, *(status.required_checks or ()))):
            return 'CI has failed or has an unsupported status'
    except (RuntimeError, ValueError, OSError, KeyError) as error:
        return f'could not verify approval eligibility: {error}'
    return ''


class ApprovalCheck:
    graph_node_name = 'Approval check'
    graph_node_kind = 'tool'
    graph_node_description = (
        'Checks Green impact, findings, CI and identity; reports why approval is skipped. '
        'A review requester with write access authorizes approval even without auto_approve_green.'
    )

    async def __call__(self, state: Mapping[str, Any]) -> dict[str, object]:
        reason = await approval_reason(state)
        summary = f'Auto-approval skipped: {reason}.' if reason else 'Eligible for automatic Green approval.'
        await current_execution().say(summary)
        return {'approval_eligible': not reason, 'approval_reason': reason}


class ApproveReview:
    graph_node_name = 'Auto-approve'
    graph_node_kind = 'tool'
    graph_node_description = (
        'Approves the PR when impact is Green 🟢, either because auto_approve_green is on '
        'or because the person who requested this review has write access to the repository.'
    )

    async def __call__(self, state: Mapping[str, Any]) -> dict[str, object]:
        execution = current_execution()
        # Recheck at the mutation boundary, including CI, identity and the requester's access.
        reason = await approval_reason(state)
        if reason:
            await execution.say(f'Auto-approval skipped: {reason}.')
            return {'approved': False, 'approval_reason': reason}
        target = change_request(target_url(state))
        assert target is not None and execution.runtime.source_control is not None
        requester = review_requester(state)
        opening = (f'Approved automatically: @{requester} requested this review and has write '
                   f'access to the repository.\n\n' if requester else 'Approved automatically.\n\n')
        body = f"{opening}{state['impact_rationale']}"
        await execution.runtime.source_control.approve_change_request(
            target.project, target.number, body, commit_sha=str(state['reviewed_sha']))
        await execution.say(body)
        return {'approved': True, 'approval_reason': body}
