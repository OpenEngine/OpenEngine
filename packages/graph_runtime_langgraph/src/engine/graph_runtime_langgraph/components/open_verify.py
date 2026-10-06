"""Run the standalone QA CLI and publish its reviewed artifact contract."""

from __future__ import annotations

import asyncio
import signal
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from engine.domain import WorkspaceId
from engine.graph_runtime_langgraph.components.workspace import checkout
from engine.graph_runtime_langgraph.executions import current_execution
from engine.ports import SourceControl
from engine.runtime.change_requests import change_request
from engine.runtime.verification import VerificationUploader, publish_verification


@dataclass(frozen=True, slots=True, kw_only=True)
class OpenVerify:
    """Opt-in workflow step; the deployment supplies installation and storage.

    The workspace is untrusted executable code, just as for implementation/CI.
    Run in a deployment-provided isolated worker with test credentials. This
    unattended node does not open an interactive OAuth session.
    """

    uploader: VerificationUploader
    output_directory: Path
    source_control: SourceControl | None = None
    command: tuple[str, ...] = ("ov",)
    timeout: float = 1800
    base_ref: str | None = None
    output_key: str = "verification"
    graph_node_name: str = "End-to-end verification"
    graph_node_kind: str = "tool"
    graph_node_description: str = (
        "Runs Open Verify and attaches test evidence to the PR."
    )

    async def __call__(self, state: Mapping[str, object]) -> dict[str, object]:
        execution = current_execution()
        source = self.source_control or execution.runtime.source_control
        if source is None:
            raise RuntimeError("OpenVerify needs SourceControl")
        workspace = state.get("workspaceId")
        pr_url = state.get("pr_url")
        target = change_request(pr_url) if isinstance(pr_url, str) else None
        if not isinstance(workspace, str) or target is None:
            raise ValueError("OpenVerify needs workspaceId and pr_url")
        current = await source.view_change_request(
            WorkspaceId(workspace), target.number
        )
        if change_request(current.url) != target:
            raise ValueError("PR does not belong to the supplied workspace")
        self.output_directory.mkdir(parents=True, exist_ok=True)
        output = Path(
            tempfile.mkdtemp(prefix="verification-", dir=self.output_directory)
        ).resolve()
        argv = [
            *self.command,
            str(state.get("task") or "Verify the changed user journeys"),
            "--project",
            checkout(state),
            "--base",
            self.base_ref or f"origin/{current.base_ref}",
            "--head",
            current.head_sha,
            "--allow-exec",
            "--headless",
            "--output",
            str(output),
        ]
        await execution.say(
            f"Verifying {pr_url} at {current.head_sha}. Evidence: {output}"
        )
        with (output / "runner.log").open("wb") as log:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=checkout(state),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log,
                stderr=log,
            )
            try:
                async with asyncio.timeout(self.timeout):
                    await process.wait()
            finally:
                if process.returncode is None:
                    # SIGINT lets the CLI finish its managed-process cleanup.
                    process.send_signal(signal.SIGINT)
                    try:
                        await asyncio.wait_for(process.wait(), 10)
                    except TimeoutError:
                        process.kill()
                        await process.wait()
        manifests = list(output.glob("*/manifest.json"))
        if len(manifests) != 1 or process.returncode not in {0, 1, 2}:
            raise RuntimeError(
                f"Open Verify did not finish a valid bundle; see {output / 'runner.log'}"
            )
        result = await publish_verification(
            manifests[0],
            workspace_id=WorkspaceId(workspace),
            pr_url=pr_url,
            source_control=source,
            uploader=self.uploader,
        )
        await execution.say(
            f"Open Verify: {result['status']}. {result['comment_url'] or 'No attachments needed.'}"
        )
        return {self.output_key: {**result, "manifest": str(manifests[0])}}
