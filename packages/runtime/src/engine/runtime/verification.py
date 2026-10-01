"""Consume Open Verify's versioned bundle without depending on its Python package."""

from __future__ import annotations

import hashlib
import html
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from engine.domain import WorkspaceId
from engine.ports import SourceControl
from engine.runtime.change_requests import change_request

MAX_VIDEO_BYTES = 10_000_000
MAX_BUNDLE_BYTES = 100_000_000
_TYPES = {
    "test": (".py", "text/x-python"),
    "screenshot": (".png", "image/png"),
    "video": (".mp4", "video/mp4"),
}


@dataclass(frozen=True)
class VerificationArtifact:
    path: str
    kind: str
    case_id: str
    media_type: str
    data: bytes

    @property
    def name(self) -> str:
        return hashlib.sha256(self.data).hexdigest() + Path(self.path).suffix


@dataclass(frozen=True)
class VerificationBundle:
    status: str
    head: str
    base: str
    tests: tuple[dict, ...]
    artifacts: tuple[VerificationArtifact, ...]
    omissions: tuple[str, ...]


class VerificationUploader(Protocol):
    async def upload(
        self, pr_url: str, head: str, artifact: VerificationArtifact
    ) -> str:
        """Return a durable HTTPS URL, reusing an identical content-addressed asset."""
        ...


def load_verification(manifest: Path, *, expected_head: str) -> VerificationBundle:
    """Validate the full publication set before any uploads or comments occur."""
    if manifest.is_symlink() or manifest.stat().st_size > 2_000_000:
        raise ValueError("Invalid verification manifest")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("Unsupported verification manifest version")
    status = data.get("status")
    if status not in {"passed", "failed", "blocked", "incomplete", "skipped"}:
        raise ValueError("Verification is not a completed execution or skip")
    change = data.get("change") or {}
    if change.get("head") != expected_head or not expected_head:
        raise ValueError("Verification does not match the current PR head")
    if change.get("include_working_tree"):
        raise ValueError(
            "Commit working-tree changes and verify that revision before publishing"
        )
    if not isinstance(change.get("base"), str) or not change["base"]:
        raise ValueError("Verification has no base revision")
    tests = data.get("tests", [])
    if not isinstance(tests, list) or len(tests) > 100:
        raise ValueError("Invalid verification tests")
    for test in tests:
        if (
            not isinstance(test, dict)
            or not isinstance(test.get("case_id"), str)
            or test.get("status") not in {"passed", "failed", "blocked"}
        ):
            raise ValueError("Invalid verification test result")
    entries = data.get("artifacts", [])
    if not isinstance(entries, list) or len(entries) > 200:
        raise ValueError("Invalid verification attachments")
    if status == "skipped" and (entries or tests):
        raise ValueError("Skipped verification must not publish attachments or tests")
    root = manifest.parent.resolve()
    artifacts, paths, size = [], set(), 0
    for entry in entries:
        relative = entry.get("path", "")
        if not isinstance(relative, str) or not relative:
            raise ValueError("Invalid artifact path")
        path = Path(relative)
        local = root / path
        if (
            path.is_absolute()
            or ".." in path.parts
            or relative in paths
            or not local.resolve().is_relative_to(root)
            or any(
                (root / Path(*path.parts[:i])).is_symlink()
                for i in range(1, len(path.parts) + 1)
            )
        ):
            raise ValueError("Artifact must be a unique file inside the bundle")
        kind = entry.get("type")
        if kind not in _TYPES or (path.suffix, entry.get("media_type")) != _TYPES[kind]:
            raise ValueError("Unsupported verification attachment type")
        if entry.get("case_id") not in {test["case_id"] for test in tests}:
            raise ValueError("Artifact has no matching test case")
        actual_size = local.stat().st_size
        limit = MAX_VIDEO_BYTES if kind == "video" else 10_000_001
        if (
            type(entry.get("size_bytes")) is not int
            or actual_size != entry["size_bytes"]
            or actual_size <= 0
            or actual_size >= limit
        ):
            raise ValueError(
                "Artifact size mismatch, empty file, or exceeded size limit"
            )
        size += actual_size
        if size > MAX_BUNDLE_BYTES:
            raise ValueError("Verification bundle exceeds publication limit")
        content = local.read_bytes()
        if len(content) != actual_size:
            raise ValueError("Artifact changed while being read")
        if (kind == "screenshot" and not content.startswith(b"\x89PNG\r\n\x1a\n")) or (
            kind == "video" and content[4:8] != b"ftyp"
        ):
            raise ValueError("Artifact content does not match its media type")
        paths.add(relative)
        artifacts.append(
            VerificationArtifact(
                relative, kind, entry["case_id"], entry["media_type"], content
            )
        )
    for test in tests:
        if test.get("path") and test["path"] not in {
            a.path for a in artifacts if a.kind == "test"
        }:
            raise ValueError("Generated test is missing from the attachment set")
    omissions = data.get("omissions", [])
    if not isinstance(omissions, list) or not all(
        isinstance(x, str) for x in omissions
    ):
        raise ValueError("Invalid verification omissions")
    return VerificationBundle(
        status,
        expected_head,
        change["base"],
        tuple(tests),
        tuple(artifacts),
        tuple(omissions),
    )


def _text(value: str) -> str:
    # Render model-provided labels as text, not markup or notifications.
    value = html.escape(value.replace("\n", " "))
    for char in "@`[]*_\\":
        value = value.replace(char, f"&#{ord(char)};")
    return value


def verification_comment(bundle: VerificationBundle, urls: dict[str, str]) -> str:
    lines = [
        "### Open Verify",
        "",
        f"Result: **{bundle.status}**",
        "",
        f"Tested revision: `{bundle.head}`",
        "",
    ]
    for test in bundle.tests:
        lines.append(f"- {_text(test['case_id'])}: {test['status']}")
    lines.append("")
    for artifact in bundle.artifacts:
        url = urls[artifact.path]
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Artifact uploader returned an invalid HTTPS URL")
        safe_url = url.replace("<", "%3C").replace(">", "%3E").replace("\n", "%0A")
        label = f"{artifact.kind} — {_text(artifact.case_id)}"
        if artifact.kind == "screenshot":
            lines.append(f"![{label}](<{safe_url}>)")
        else:
            lines.append(f"[{label}](<{safe_url}>)")
        lines.append("")
    if bundle.omissions:
        lines.extend(
            ["Evidence omissions:", *[f"- {_text(x)}" for x in bundle.omissions]]
        )
    return "\n".join(lines)


async def publish_verification(
    manifest: Path,
    *,
    workspace_id: WorkspaceId,
    pr_url: str,
    source_control: SourceControl,
    uploader: VerificationUploader,
):
    target = change_request(pr_url)
    if target is None:
        raise ValueError("Expected a pull/merge request URL")
    current = await source_control.view_change_request(workspace_id, target.number)
    if change_request(current.url) != target:
        raise ValueError("PR does not belong to the verification workspace")
    bundle = load_verification(manifest, expected_head=current.head_sha)
    if bundle.status == "skipped":
        return {"status": "skipped", "comment_url": None, "artifacts": []}
    urls = {}
    for artifact in bundle.artifacts:
        urls[artifact.path] = await uploader.upload(pr_url, bundle.head, artifact)
    body = verification_comment(bundle, urls)
    marker = "<!-- open-verify:" + hashlib.sha256(body.encode()).hexdigest() + " -->"
    body += "\n\n" + marker
    # Uploads can take time; don't attach old evidence as if it covered a new push.
    refreshed = await source_control.view_change_request(workspace_id, target.number)
    if refreshed.head_sha != bundle.head:
        raise ValueError("PR head changed during publication; rerun verification")
    for comment in refreshed.comments:
        if marker in comment.body:
            return {
                "status": bundle.status,
                "comment_url": comment.url,
                "artifacts": list(urls.values()),
            }
    comment = await source_control.add_comment(pr_url, body)
    return {
        "status": bundle.status,
        "comment_url": comment.url,
        "artifacts": list(urls.values()),
    }
