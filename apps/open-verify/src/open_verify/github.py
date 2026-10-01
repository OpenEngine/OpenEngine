"""Small standalone GitHub adapter using the user's existing gh authentication."""

import asyncio
import hashlib
import html
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from open_verify.manifest import Manifest, bundle_file
from open_verify.media import MAX_VIDEO_BYTES


async def command(argv, *, cwd=None, data=None, timeout=120):
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
            stdout=output,
            stderr=error,
        )
        try:
            await asyncio.wait_for(process.communicate(data), timeout)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        error.seek(0)
        if process.returncode:
            raise RuntimeError(
                error.read(4000).decode("utf-8", "replace").strip() or f"{argv[0]} failed"
            )
        output.seek(0)
        result = output.read(2_000_001)
        if len(result) > 2_000_000:
            raise ValueError("Command response exceeded its output limit")
        return result.decode("utf-8", "replace")


def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("GitHub returned an invalid commit SHA")
    return value


@dataclass(frozen=True)
class PullRequest:
    repository: str
    number: int
    head: str = ""
    base: str = ""
    merge_base: str = ""

    @property
    def url(self):
        return f"https://github.com/{self.repository}/pull/{self.number}"

    @classmethod
    def parse(cls, url):
        match = re.fullmatch(
            r"https://github\.com/([\w.-]+)/([\w.-]+)/pull/([1-9][0-9]*)/?", url, re.ASCII
        )
        if not match or any(p in {".", ".."} for p in match.groups()[:2]):
            raise ValueError("--pr must be a github.com pull request URL")
        return cls(f"{match[1]}/{match[2]}", int(match[3]))


class GitHub:
    async def api(self, path, *, method="GET", body=None, data=None, media_type=None, select=None):
        argv = ["gh", "api", path, "--hostname", "github.com", "--method", method]
        if select:
            argv.extend(["--jq", select])
        if body is not None:
            data = json.dumps(body).encode()
            media_type = media_type or "application/json"
        if data is not None:
            # stdin has no known length; GitHub release uploads reject chunked bodies.
            argv.extend(["--input", "-", "--header", f"Content-Length: {len(data)}"])
        if media_type:
            argv.extend(["--header", "Content-Type: " + media_type])
        result = await command(argv, data=data)
        return json.loads(result) if result.strip() else None

    async def snapshot(self, target):
        value = await self.api(
            f"/repos/{target.repository}/pulls/{target.number}",
            select="{head: .head.sha, base: .base.sha, state: .state}",
        )
        if value["state"] != "open":
            raise ValueError("The pull request is no longer open")
        return PullRequest(target.repository, target.number, sha(value["head"]), sha(value["base"]))

    async def resolve(self, target):
        current = await self.snapshot(target)
        comparison = await self.api(
            f"/repos/{target.repository}/compare/{current.base}...{current.head}",
            select="{merge_base: .merge_base_commit.sha}",
        )
        return PullRequest(
            current.repository,
            current.number,
            current.head,
            current.base,
            sha(comparison["merge_base"]),
        )

    async def ensure_current(self, target):
        current = await self.snapshot(target)
        if (current.head, current.base) != (target.head, target.base):
            raise ValueError("PR head or base changed during verification; rerun before publishing")

    async def publish(self, manifest_path: Path, target: PullRequest):
        if manifest_path.is_symlink() or manifest_path.stat().st_size > 2_000_000:
            raise ValueError("Invalid publication manifest")
        manifest = Manifest.model_validate_json(manifest_path.read_text())
        change = manifest.change
        if (
            not change
            or (change.head, change.base) != (target.head, target.merge_base)
            or change.include_working_tree
        ):
            raise ValueError("Bundle does not describe the tested PR revision")
        if manifest.status == "planned":
            raise ValueError("A plan cannot be published as test evidence")
        await self.ensure_current(target)
        if manifest.status == "skipped":
            if manifest.artifacts:
                raise ValueError("Skipped verification cannot include attachments")
            return None
        # Validate every file before the first remote write; retain byte snapshots
        # so a changed on-disk file cannot silently become the uploaded evidence.
        files, total = [], 0
        types = {
            "test": (".py", "text/x-python"),
            "screenshot": (".png", "image/png"),
            "video": (".mp4", "video/mp4"),
        }
        for item in manifest.artifacts:
            path = bundle_file(manifest_path.parent, item.path)
            if (path.suffix, item.media_type) != types[
                item.type
            ] or path.stat().st_size != item.size_bytes:
                raise ValueError("Artifact type or size does not match the manifest")
            if not 0 < item.size_bytes <= 10_000_000 or (
                item.type == "video" and item.size_bytes >= MAX_VIDEO_BYTES
            ):
                raise ValueError("Empty or oversized evidence file")
            total += item.size_bytes
            if total > 100_000_000:
                raise ValueError("Evidence bundle is too large")
            data = path.read_bytes()
            if len(data) != item.size_bytes:
                raise ValueError("Evidence changed while reading")
            if (item.type == "screenshot" and not data.startswith(b"\x89PNG\r\n\x1a\n")) or (
                item.type == "video" and data[4:8] != b"ftyp"
            ):
                raise ValueError("Invalid media file")
            files.append((item, data, hashlib.sha256(data).hexdigest() + path.suffix))
        release = None
        prefix = f"/repos/{target.repository}"
        tag = f"open-verify/pr-{target.number}/{target.head}"
        if files:
            # List by pages to distinguish a missing release from an API outage.
            page = 1
            while True:
                releases = await self.api(f"{prefix}/releases?per_page=100&page={page}")
                release = next((r for r in releases if r["tag_name"] == tag), None)
                if release or len(releases) < 100:
                    break
                page += 1
            if release is None:
                release = await self.api(
                    f"{prefix}/releases",
                    method="POST",
                    body={
                        "tag_name": tag,
                        "target_commitish": target.head,
                        "prerelease": True,
                        "make_latest": "false",
                        "name": f"Open Verify: PR #{target.number} ({target.head[:12]})",
                        "body": "End-to-end evidence produced by Open Verify.",
                    },
                )
            if type(release.get("id")) is not int or release.get("draft"):
                raise ValueError("Invalid evidence release")
        assets, page = {}, 1
        if release:
            while True:
                batch = await self.api(
                    f"{prefix}/releases/{release['id']}/assets?per_page=100&page={page}"
                )
                assets.update((a["name"], a) for a in batch)
                if len(batch) < 100:
                    break
                page += 1
        lines = [
            "### Open Verify",
            "",
            f"Result: **{manifest.status}**",
            "",
            f"Tested revision: `{target.head}`",
            "",
        ]
        for test in manifest.tests:
            lines.append(f"- {safe_text(test.case_id)}: {test.status}")
        for item, data, name in files:
            asset = assets.get(name)
            if asset is not None and asset.get("state") == "starter" and type(asset.get("id")) is int:
                # GitHub can retain an empty asset after a failed upload.
                await self.ensure_current(target)
                await self.api(f"{prefix}/releases/assets/{asset['id']}", method="DELETE")
                asset = None
            if asset is None:
                asset = await self.api(
                    f"https://uploads.github.com/repos/{target.repository}/releases/{release['id']}/assets?name={quote(name)}",
                    method="POST",
                    data=data,
                    media_type=item.media_type,
                )
                assets[name] = asset
            url = asset.get("browser_download_url", "")
            if (
                asset.get("size") != len(data)
                or asset.get("state") != "uploaded"
                or not url.startswith(f"https://github.com/{target.repository}/releases/download/")
            ):
                raise ValueError("GitHub asset failed validation")
            if asset.get("digest") and asset["digest"] != "sha256:" + name.split(".")[0]:
                raise ValueError("GitHub asset digest mismatch")
            url = url.replace("<", "%3C").replace(">", "%3E").replace("\n", "%0A")
            label = f"{item.type} — {safe_text(item.case_id)}"
            lines.extend(["", f"{'!' if item.type == 'screenshot' else ''}[{label}](<{url}>)"])
        if manifest.reason:
            lines.extend(["", safe_text(manifest.reason)])
        if manifest.omissions:
            lines.extend(
                ["", "Evidence omissions:", *[f"- {safe_text(x)}" for x in manifest.omissions]]
            )
        body = "\n".join(lines)
        marker = "<!-- open-verify:" + hashlib.sha256(body.encode()).hexdigest() + " -->"
        await self.ensure_current(target)
        page = 1
        while True:
            comments = await self.api(
                f"{prefix}/issues/{target.number}/comments?per_page=100&page={page}"
            )
            existing = next((c for c in comments if marker in c.get("body", "")), None)
            if existing:
                return existing["html_url"]
            if len(comments) < 100:
                break
            page += 1
        await self.ensure_current(target)
        comment = await self.api(
            f"{prefix}/issues/{target.number}/comments",
            method="POST",
            body={"body": body + "\n\n" + marker},
        )
        return comment["html_url"]


def safe_text(value):
    text = html.escape(value.replace("\n", " "))
    for char in "@`[]*_\\":
        text = text.replace(char, f"&#{ord(char)};")
    return text


async def publish_saved(directory: Path, *, github=None):
    """Retry publication from immutable revision metadata, without fetching or testing."""
    directory = directory.resolve()
    github = github or GitHub()
    try:
        if (directory / "cleanup-errors.json").exists() or (directory / "pr-cleanup-error.json").exists():
            raise ValueError("Runtime/worktree cleanup failed; inspect the run before publishing")
        metadata = directory / "pull-request.json"
        if metadata.is_symlink() or metadata.stat().st_size > 10000:
            raise ValueError("Invalid saved PR metadata")
        data = json.loads(metadata.read_text())
        target = PullRequest.parse(f"https://github.com/{data['repository']}/pull/{data['number']}")
        target = PullRequest(target.repository, target.number, sha(data['head']), sha(data['base']), sha(data['merge_base']))
        print(f"Publish: checking saved evidence for {target.url}", flush=True)
        url = await github.publish(directory / "manifest.json", target)
        publication = {"status": "published" if url else "skipped", "comment_url": url}
        print(f"PR comment: {url}" if url else "Publication skipped: no relevant change.", flush=True)
    except Exception as exc:
        publication = {"status": "blocked", "error": str(exc)}
        print(f"Publication blocked: {exc}", flush=True)
    if directory.is_dir():
        (directory / "publication.json").write_text(json.dumps(publication, indent=2) + "\n")
    return 2 if publication["status"] == "blocked" else 0
