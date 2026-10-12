"""Resolve CLI targets on the backend which will run them."""
from __future__ import annotations

import asyncio
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any

from engine.runtime.change_requests import remote_project
from engine.graph_runtime_langgraph.forge import resolve_forge_context
from engine.graph_service.service import NotFound, ServiceError

NO_TARGET = 'no such branch or PR; to give an instruction, quote it'
NO_REPOSITORY = 'no repository: run in a checkout or pass --repo or a full PR URL'
CONNECT = 'cannot read this repository while disconnected; run engine connect github'
FORK_FIX = 'cannot fix a fork PR or a PR with an unavailable head repository; check out the fork and fix it there'
PRIVATE = 'private repository requires a forge connection; run engine connect github'

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
                         local: bool = False, verb: str = 'review') -> dict[str, Any]:
    """One resolver for URLs, qualified/unqualified numbers, branches and inference."""
    number = 0
    match = re.fullmatch(r'https://github\.com/([^/]+/[^/]+)/pull/([1-9][0-9]*)/?', target)
    shorthand = re.fullmatch(r'(?:(\w[\w.-]*/[\w.-]+))?#([1-9][0-9]*)', target)
    if match or shorthand:
        chosen = match or shorthand
        assert chosen is not None
        repository = chosen[1] or repository
        number = int(chosen[2])
        target = ''
    elif target.startswith(('https://', '#')):
        raise NotFound(NO_TARGET)
    path = service.resolve_repository(repository)
    checkout = Path(path).is_dir() if path else False
    project = remote_project(await _git(path, 'remote', 'get-url', 'origin')) if checkout else remote_project(repository)
    project = project or (repository if re.fullmatch(r'[^/]+/[^/]+', repository) else '')
    if not repository:
        raise ServiceError(NO_REPOSITORY)
    if not target and not number and verb != 'go' and checkout:
        target = await _git(path, 'symbolic-ref', '--quiet', '--short', 'HEAD')
    context = await resolve_forge_context(service.runtime.source_control,
        {'repository': path, 'inputs': {'mode': 'disconnected'} if local else {}})
    disconnected = context['mode'] == 'disconnected'
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
    if verb == 'fix' and details.get('pr_url') and str(details.get('head_repository', '')).casefold() != project.casefold():
        raise ServiceError(FORK_FIX)
    exists = bool(target and checkout and await _git(path, 'show-ref', '--verify', '--hash', f'refs/heads/{target}'))
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
