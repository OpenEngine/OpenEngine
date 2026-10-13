"""Resolve CLI targets on the backend which will run them."""
from __future__ import annotations

import asyncio
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any

from engine.runtime.change_requests import change_request, remote_project
from engine.graph_runtime_langgraph.forge import resolve_forge_context
from engine.graph_service.service import NotFound, ServiceError

NO_TARGET = 'no such branch or PR; to give an instruction, quote it'
NO_REPOSITORY = 'no repository: run in a checkout or pass --repo or a full PR URL'
CONNECT = 'cannot read this repository while disconnected; run engine connect github'
FORK_FIX = 'cannot fix a fork PR or a PR with an unavailable head repository; check out the fork and fix it there'
PRIVATE = 'private repository requires a forge connection; run engine connect github'
OUTSIDE_REPOSITORY = 'PR target is outside the configured repository'

if TYPE_CHECKING:
    from engine.graph_service.service import GraphService


async def _git(path: str, *args: str) -> str:
    process = await asyncio.create_subprocess_exec('git', '-C', path, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 10)
    except TimeoutError:
        process.kill()
        await process.wait()
        return ''
    return output.decode().strip() if process.returncode == 0 else ''


async def resolve_target(service: GraphService, repository: str, target: str, *,
                         local: bool = False, verb: str = 'review', restrict_repository: bool = False) -> dict[str, Any]:
    """One resolver for URLs, qualified/unqualified numbers, branches and inference."""
    allowed_project = ''
    if restrict_repository:
        configured_path = service.resolve_repository(repository)
        remote = (await _git(configured_path, 'remote', 'get-url', 'origin')
                  if configured_path and Path(configured_path).is_dir() else repository)
        allowed_project = remote_project(remote) or (
            remote.lower().removesuffix('.git')
            if re.fullmatch(r'[\w.-]+/[\w.-]+', remote) else ''
        )
        if not allowed_project:
            raise ServiceError(OUTSIDE_REPOSITORY)
    number = 0
    parsed = change_request(target)
    shorthand = re.fullmatch(r'(?:(\w[\w.-]*/[\w.-]+))?#([1-9][0-9]*)', target)
    if parsed and parsed.kind == 'pull' and parsed.host == 'github.com' and parsed.project == parsed.path.lower():
        repository, number, target = parsed.path, parsed.number, ''
    elif shorthand:
        repository, number, target = shorthand[1] or repository, int(shorthand[2]), ''
    elif target.startswith(('https://', 'http://', '#')):
        raise NotFound(NO_TARGET)
    path = service.resolve_repository(repository)
    checkout = Path(path).is_dir() if path else False
    project = remote_project(await _git(path, 'remote', 'get-url', 'origin')) if checkout else remote_project(repository)
    project = project or (repository if re.fullmatch(r'[^/]+/[^/]+', repository) else '')
    if restrict_repository and project.lower() != allowed_project:
        raise ServiceError(OUTSIDE_REPOSITORY)
    if not repository:
        raise ServiceError(NO_REPOSITORY)
    context = await resolve_forge_context(service.runtime.source_control,
        {'repository': path, 'inputs': {'mode': 'disconnected'} if local else {}})
    disconnected = context['mode'] == 'disconnected'
    if not target and not number and checkout and (verb != 'go' or disconnected):
        target = await _git(path, 'symbolic-ref', '--quiet', '--short', 'HEAD')
        if not target and disconnected:
            raise ServiceError('no current branch; switch to a branch or pass one explicitly')
    exists = bool(target and checkout and await _git(path, 'show-ref', '--verify', '--hash', f'refs/heads/{target}'))
    if disconnected and verb in {'review', 'go'} and exists and not number:
        return {'repo': repository, 'mode': context['mode'], 'ref': target}
    inspector = getattr(service.runtime.source_control, 'inspect_target', None)
    details: dict[str, Any] = {}
    if project and inspector:
        try:
            details = await inspector(project, number=number, branch=target, include_threads=verb == 'fix')
        except Exception as error:
            if disconnected:
                raise ServiceError(CONNECT) from error
            raise NotFound(NO_TARGET) from error
        if disconnected and details.get('private'):
            raise ServiceError(PRIVATE)
    if restrict_repository and details.get('pr_url'):
        resolved_pr = change_request(details['pr_url'])
        if resolved_pr is None or resolved_pr.project != allowed_project:
            raise ServiceError(OUTSIDE_REPOSITORY)
    if verb == 'fix' and details.get('pr_url') and str(details.get('head_repository', '')).casefold() != project.casefold():
        raise ServiceError(FORK_FIX)
    if target and not exists and not details.get('exists'):
        raise NotFound(NO_TARGET)
    if number and not details.get('pr_url'):
        raise NotFound(NO_TARGET)
    if verb != 'go' and not target and not number:
        raise NotFound(NO_TARGET)
    result: dict[str, Any] = {'repo': repository, 'mode': context['mode']}
    branch = details.get('branch') or target
    if branch:
        result['ref'] = branch if exists else f'refs/remotes/origin/{branch}' if disconnected else f'origin/{branch}'
    if verb != 'fix' and not disconnected and details.get('checkout_ref'):
        result['ref'] = details['checkout_ref']
    if details.get('pr_url'):
        result['pr_url'] = details['pr_url']
    if verb == 'fix' and 'review_threads' in details:
        result['review_threads'] = details['review_threads']
    return result
