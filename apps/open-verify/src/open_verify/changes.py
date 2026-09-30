"""Read-only change discovery; no checkout, fetch, or repository mutation."""

import asyncio
import hashlib
import os
import tempfile
from pathlib import Path

from pydantic import Field

from open_verify.models import Contract
from open_verify.scope import inspectable

MAX_DIFF = 120_000
MAX_GIT_OUTPUT = 4_000_000


class ChangeReference(Contract):
    base: str
    head: str
    include_working_tree: bool = False
    files: list[str] = Field(default_factory=list)
    excluded_files: list[str] = Field(default_factory=list)
    truncated: bool = False
    fingerprint: str = ""


class Change(ChangeReference):
    diff: str = ""


class GitReader:
    """Fixed argv commands, bounded runtime/output, and no external diff helpers."""

    def __init__(self, project: Path):
        self.project = project

    async def read(self, *args: str) -> str:
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
            process = await asyncio.create_subprocess_exec(
                "git",
                "--no-optional-locks",
                "-c",
                "core.fsmonitor=false",
                *args,
                cwd=self.project,
                stdout=output,
                stderr=error,
                stdin=asyncio.subprocess.DEVNULL,
                **({"creationflags": 0x08000000} if os.name == "nt" else {}),
            )
            try:
                await asyncio.wait_for(process.wait(), 30)
            except BaseException:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                raise
            if process.returncode:
                error.seek(0)
                raise ValueError(
                    "Cannot read change: " + error.read(4000).decode("utf-8", "replace")
                )
            output.seek(0)
            data = output.read(MAX_GIT_OUTPUT + 1)
            if len(data) > MAX_GIT_OUTPUT:
                raise ValueError("Change is too large; select a smaller revision range")
            return data.decode("utf-8", "replace")


async def read_change(
    project: Path,
    base: str,
    head: str = "HEAD",
    *,
    include_working_tree=False,
    reader: GitReader | None = None,
) -> Change:
    git = reader or GitReader(project)
    base_sha = (
        await git.read("rev-parse", "--verify", "--end-of-options", base + "^{commit}")
    ).strip()
    head_sha = (
        await git.read("rev-parse", "--verify", "--end-of-options", head + "^{commit}")
    ).strip()
    checkout = (await git.read("rev-parse", "--verify", "HEAD^{commit}")).strip()
    if checkout != head_sha:
        raise ValueError(
            "--head must match the current checkout; Open Verify does not switch branches"
        )
    dirty = await git.read("status", "--porcelain=v1", "-z", "--untracked-files=all")
    if dirty and not include_working_tree:
        raise ValueError(
            "Checkout has local changes; use --include-working-tree or a clean checkout"
        )
    # base -> worktree includes both staged and unstaged tracked changes.
    revisions = [base_sha] if include_working_tree else [base_sha, head_sha]
    diff_args = ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--no-color"]
    paths = (await git.read(*diff_args, "--name-only", "-z", *revisions, "--")).split("\0")
    # Binary patches omit the content. Use machine-readable metadata rather
    # than the human-readable (and potentially localized) "Binary files" notice.
    stats = await git.read(*diff_args, "--numstat", "-z", *revisions, "--")
    binary_paths = {
        entry.split("\t", 2)[2] for entry in stats.split("\0") if entry.startswith("-\t-\t")
    }
    untracked = []
    if include_working_tree:
        untracked = (await git.read("ls-files", "--others", "--exclude-standard", "-z")).split("\0")
    paths = sorted(set(filter(None, paths + untracked)))
    included = [path for path in paths if inspectable(path)]
    excluded = [path for path in paths if path not in included]
    chunks = []
    inspected_size = 0
    truncated = False
    for index, path in enumerate(included):
        if inspected_size >= MAX_DIFF or index >= 200:
            truncated = True
            break
        local = (project / path).resolve()
        if not local.is_relative_to(project.resolve()) or (project / path).is_symlink():
            excluded.append(path)
            continue
        if path in untracked:
            with local.open("rb") as stream:
                data = stream.read(MAX_DIFF + 1)
            if b"\0" in data:
                chunks.append(f"\nBinary untracked file: {path} (content omitted)\n")
                truncated = True
            else:
                chunks.append(f"\nUntracked file: {path}\n" + data.decode("utf-8", "replace"))
        else:
            if path in binary_paths:
                truncated = True
            chunks.append(
                await git.read(*diff_args, "--unified=3", *revisions, "--", ":(literal)" + path)
            )
        inspected_size += len(chunks[-1])
    diff = "\n".join(chunks)
    return Change(
        base=base_sha,
        head=head_sha,
        include_working_tree=include_working_tree,
        files=[path for path in included if path not in excluded],
        excluded_files=excluded,
        diff=diff[:MAX_DIFF],
        truncated=truncated or len(diff) > MAX_DIFF,
        fingerprint=hashlib.sha256(diff[:MAX_DIFF].encode()).hexdigest(),
    )
