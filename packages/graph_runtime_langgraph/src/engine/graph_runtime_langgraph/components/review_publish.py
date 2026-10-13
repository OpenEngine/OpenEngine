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
        await source.add_comment(target_url(state),
            f"Impact: {state['impact_level']}\n\n{state['impact_rationale']}")
        await execution.say('Published review findings and impact summary.')
        return {'review_published': True}


async def approval_reason(state: Mapping[str, Any]) -> str:
    """Empty means eligible. Unknown forge data fails closed with a visible reason."""
    if not enabled(state, 'auto_approve_green'):
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
        workspace = WorkspaceId(str(state['workspaceId']))
        change = await source.view_change_request(workspace, target.number)
        # The workspace-scoped lookup must name the exact PR we will approve.
        if change_request(change.url) != target:
            return 'target PR does not match the workspace repository'
        reviewed_sha = state.get('reviewed_sha')
        if not reviewed_sha or reviewed_sha != change.head_sha:
            return 'reviewed commit does not match the PR head'
        login = await source.authenticated_login(target_url(state))
        if not login or not change.author:
            return 'approving identity or PR author is unavailable'
        if login.casefold() == change.author.casefold():
            return 'PR author is the approving identity'
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
    graph_node_description = 'Checks Green impact, findings, CI and identity; reports why approval is skipped.'

    async def __call__(self, state: Mapping[str, Any]) -> dict[str, object]:
        reason = await approval_reason(state)
        summary = f'Auto-approval skipped: {reason}.' if reason else 'Eligible for automatic Green approval.'
        await current_execution().say(summary)
        return {'approval_eligible': not reason, 'approval_reason': reason}


class ApproveReview:
    graph_node_name = 'Auto-approve'
    graph_node_kind = 'tool'
    graph_node_description = 'Approves the PR when impact is Green 🟢.'

    async def __call__(self, state: Mapping[str, Any]) -> dict[str, object]:
        execution = current_execution()
        # Recheck at the mutation boundary, including CI and self-approval.
        reason = await approval_reason(state)
        if reason:
            await execution.say(f'Auto-approval skipped: {reason}.')
            return {'approved': False, 'approval_reason': reason}
        target = change_request(target_url(state))
        assert target is not None and execution.runtime.source_control is not None
        body = f"Approved automatically because impact was Green 🟢.\n\n{state['impact_rationale']}"
        await execution.runtime.source_control.approve_change_request(
            target.project, target.number, body, commit_sha=str(state['reviewed_sha']))
        await execution.say(body)
        return {'approved': True, 'approval_reason': body}
