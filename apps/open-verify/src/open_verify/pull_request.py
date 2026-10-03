"""PR orchestration without switching or modifying the user's checkout."""

import copy
import os
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path

from open_verify.artifacts import Artifacts
from open_verify.github import GitHub, PullRequest, command
from open_verify.manifest import write_manifest


class PullRequestWorkspace:
    def __init__(self, target):
        self.target = target
        self.root = Path(tempfile.mkdtemp(prefix="open-verify-pr-"))
        self.repository = self.root / "repository.git"
        self.path = self.root / "checkout"
        self.hooks = self.root / "empty-hooks"
        self.hooks.mkdir()

    async def git(self, *args):
        return await command(
            [
                "git",
                "-c",
                f"core.hooksPath={self.hooks}",
                "-c",
                "credential.helper=",
                "-c",
                "credential.helper=!gh auth git-credential",
                *args,
            ],
            timeout=180,
        )

    async def prepare(self):
        target = self.target
        await self.git("init", "--bare", str(self.repository))
        await self.git(
            "--git-dir",
            str(self.repository),
            "remote",
            "add",
            "origin",
            f"https://github.com/{target.repository}.git",
        )
        await self.git(
            "--git-dir",
            str(self.repository),
            "fetch",
            "--no-tags",
            "--depth=1",
            "origin",
            f"refs/pull/{target.number}/head:refs/ov/head",
            f"{target.base}:refs/ov/base",
            f"{target.merge_base}:refs/ov/merge-base",
        )
        head = (
            await self.git("--git-dir", str(self.repository), "rev-parse", "refs/ov/head")
        ).strip()
        if head != target.head:
            raise ValueError("PR changed while fetching; rerun verification")
        await self.git(
            "--git-dir",
            str(self.repository),
            "worktree",
            "add",
            "--detach",
            str(self.path),
            target.head,
        )
        return self.path

    async def close(self):
        if self.path.exists() and (self.path / ".git").exists():
            await self.git(
                "--git-dir", str(self.repository), "worktree", "remove", "--force", str(self.path)
            )
        shutil.rmtree(self.root)


def setup_files(source: Path, names: list[str]):
    root = source.resolve()
    files = []
    for name in names:
        relative = Path(name)
        path = root / relative
        if (
            relative.is_absolute()
            or not relative.parts
            or any(p in {"..", ".git"} for p in relative.parts)
            or not path.resolve().is_relative_to(root)
            or not path.is_file()
            or path.stat().st_size > 1_000_000
        ):
            raise ValueError("--setup-file must name a small file inside --project, outside .git")
        files.append((path, relative))
    return files


def copy_setup(files, destination):
    for source, relative in files:
        path = destination / relative
        if (
            path.exists()
            or path.is_symlink()
            or not path.resolve().is_relative_to(destination.resolve())
        ):
            raise ValueError(
                f"Setup file would overwrite PR content or escape the checkout: {relative}"
            )
        # Explicit local configuration only; never import source-branch code,
        # node_modules, virtualenvs, or an entire browser profile into the PR.
        path.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
            output.write(source.read_bytes())


async def run_pull_request(args, verify, *, github=None, workspace_factory=PullRequestWorkspace):
    artifacts = Artifacts(args.output.resolve())
    github = github or GitHub()
    workspace = None
    result = 2
    completed = False
    try:
        files = setup_files(args.project, args.setup_file)
        target = PullRequest.parse(args.pr)
        print(f"PR: {target.url}\nArtifacts: {artifacts.path}", flush=True)
        print("PR setup: resolving base, head, and merge base…", flush=True)
        target = await github.resolve(target)
        artifacts.write("pull-request.json", asdict(target))
        print(
            f"PR setup: fetching commit {target.head[:12]} into an isolated worktree…", flush=True
        )
        workspace = workspace_factory(target)
        project = await workspace.prepare()
        await github.ensure_current(target)
        local = copy.copy(args)
        local.project, local.base, local.head = project, target.merge_base, target.head
        local.include_working_tree = False

        def prepare():
            if files:
                print("PR setup: copying explicitly requested local configuration…", flush=True)
                copy_setup(files, project)

        # Snapshot the committed diff before introducing private runtime setup.
        result = await verify(local, artifacts=artifacts, prepare=prepare)
        completed = True
    except Exception as exc:
        artifacts.write("pr-status.json", {"status": "blocked", "error": str(exc)})
        if not (artifacts.path / "manifest.json").exists():
            report = {
                "request": args.request,
                "status": "blocked",
                "findings": [],
                "note": str(exc),
            }
            artifacts.report(report)
            write_manifest(artifacts.path, report, None, [])
        print(f"PR verification blocked: {exc}", flush=True)
    finally:
        if workspace is not None:
            print("PR cleanup: removing the temporary worktree…", flush=True)
            try:
                await workspace.close()
            except Exception as exc:
                completed = False
                artifacts.write(
                    "pr-cleanup-error.json", {"error": str(exc), "workspace": str(workspace.path)}
                )
                print(f"PR cleanup failed; retained {workspace.root}: {exc}", flush=True)
        print(f"Manifest: {artifacts.path / 'manifest.json'}", flush=True)
    if not completed:
        return 2
    if args.publish:
        try:
            if (artifacts.path / "cleanup-errors.json").exists():
                raise ValueError("Runtime cleanup failed; inspect the run before publishing")
            print("Publish: checking the PR revision and uploading test evidence…", flush=True)
            url = await github.publish(artifacts.path / "manifest.json", target)
            artifacts.write(
                "publication.json",
                {"status": "published" if url else "skipped", "comment_url": url},
            )
            print(
                f"PR comment: {url}" if url else "Publish: skipped; no evidence needed.", flush=True
            )
        except Exception as exc:
            artifacts.write("publication.json", {"status": "blocked", "error": str(exc)})
            print(f"Publication blocked: {exc}", flush=True)
            print(f"Retry publication without testing: ov --publish-from {str(artifacts.path)!r}", flush=True)
            return 2
    return result
