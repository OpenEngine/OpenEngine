"""Resolve forge access once, before any node in a run executes."""
from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

from engine.ports import SourceControl
from engine.runtime.change_requests import remote_project


async def resolve_forge_context(
    source: SourceControl | None, values: Mapping[str, object],
) -> dict[str, str]:
    repository = str(values.get("repository") or "")
    inputs = values.get("inputs")
    inputs = inputs if isinstance(inputs, Mapping) else {}
    mode = "disconnected"
    reason = "no forge connection"
    project = repository
    if inputs.get("mode") == "disconnected":
        reason = "explicitly requested local execution"
    elif not repository:
        reason = "run has no repository"
    elif source is not None:
        if Path(repository).expanduser().is_dir():
            process = await asyncio.create_subprocess_exec(
                "git", "-C", str(Path(repository).expanduser()), "remote", "get-url", "origin",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
                project = remote_project(stdout.decode().strip()) or ""
            except TimeoutError:
                process.kill()
                await process.wait()
                project = ""
        else:
            project = remote_project(repository) or repository
        if project and not project.startswith(("/", ".", "~")) and "/" in project:
            url = "https://" + (project if project.count("/") > 1 else "github.com/" + project)
            try:
                async with asyncio.timeout(5):
                    login = await source.authenticated_login(url)
                    if login:
                        reason = "forge connection cannot write to repository"
                        if await source.can_write_repository(url, login):
                            mode = "connected"
                            reason = "forge connection has repository write access"
            except Exception:  # noqa: BLE001 -- failed access probes select local execution
                reason = "forge access could not be verified"
        else:
            reason = "repository has no forge remote"
    summary = (
        f"connected · {project} · will open a PR and post review comments"
        if mode == "connected" else
        "disconnected · local branch only (run oe connect github to publish)"
    )
    return {"mode": mode, "reason": reason, "repository": project or repository, "summary": summary}
