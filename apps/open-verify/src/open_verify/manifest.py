"""Versioned OE publication contract; diagnostics stay outside attachments."""

import platform
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal

from pydantic import Field

from open_verify.changes import Change, ChangeReference
from open_verify.media import MAX_VIDEO_BYTES
from open_verify.models import Contract, Finding, Impact
from open_verify.test_spec import Checkpoint, TestResult


class Attachment(Contract):
    type: Literal["test", "screenshot", "video"]
    path: str
    size_bytes: int
    case_id: str
    media_type: str


class ManifestTest(Contract):
    case_id: str
    runner: str
    status: Literal["passed", "failed", "blocked"]
    detail: str
    path: str | None
    rerun: list[str]
    cwd: Literal["."] = "."
    title: str = ""
    coverage: Literal["changed_behavior", "regression", "requested_behavior"] = "requested_behavior"
    verification: Literal["live", "existing_tests"] = "live"
    interaction: Literal["user", "library"] = "user"
    scripted_providers: bool = False
    checks: list[str] = Field(default_factory=list)
    checkpoints: list[Checkpoint] = Field(default_factory=list)


class Manifest(Contract):
    schema_version: Literal[1] = 1
    status: Literal["passed", "failed", "blocked", "incomplete", "skipped", "planned"]
    reason: str
    change: ChangeReference | None
    impact: Impact | None
    environment: dict[str, str]
    tests: list[ManifestTest] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    artifacts: list[Attachment] = Field(default_factory=list)
    support_files: list[str] = Field(default_factory=list)
    omissions: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


def bundle_file(root: Path, relative: str) -> Path:
    path = Path(relative)
    resolved = (root / path).resolve()
    if (
        path.is_absolute()
        or ".." in path.parts
        or not resolved.is_relative_to(root.resolve())
        or not resolved.is_file()
    ):
        raise ValueError("Artifact must be an existing file inside the bundle: " + relative)
    return resolved


def outcome(report: dict, cleanup_errors: list[str]) -> str:
    if cleanup_errors or report["status"] in {"error", "blocked"}:
        return "blocked"
    if report["status"] in {"skipped", "planned"}:
        return report["status"]
    if any(item["status"] == "failed" for item in report["findings"]):
        return "failed"
    if any(item["status"] == "blocked" for item in report["findings"]):
        return "blocked"
    if report["status"] == "complete" and all(
        item["status"] == "passed" for item in report["findings"]
    ):
        return "passed"
    return "incomplete"


def write_manifest(
    root: Path,
    report: dict,
    change: Change | None,
    results: list[TestResult],
    *,
    cleanup_errors=(),
) -> Manifest:
    environment = {"python": sys.version.split()[0], "platform": platform.platform()}
    for package in ("open-verify", "playwright", "langgraph-acp"):
        try:
            environment[package] = version(package)
        except PackageNotFoundError:
            environment[package] = "not installed"
    manifest = Manifest(
        status=outcome(report, list(cleanup_errors)),
        reason=report.get("note", ""),
        change=change.model_dump(exclude={"diff"}) if change else None,
        impact=report.get("impact"),
        environment=environment,
        findings=report.get("findings", []),
        omissions=list(cleanup_errors),
        assumptions=(report.get("plan") or {}).get("assumptions", []),
    )
    if manifest.status not in {"skipped", "planned"}:
        latest_results = {result.case_id: result for result in results}
        for result in latest_results.values():
            case = next((c for c in (report.get("plan") or {}).get("cases", [])
                         if c["id"] == result.case_id), {})
            manifest.omissions.extend(result.omissions)
            attachments = [(result.test_file, "test", "text/x-python")]
            if result.screenshots or result.videos:
                attachments.extend((p, "screenshot", "image/gif" if Path(p).suffix == '.gif' else "image/png") for p in result.screenshots)
                attachments.extend((p, "video", "video/mp4") for p in result.videos)
            accepted_test = None
            for relative, kind, mime in attachments:
                try:
                    path = bundle_file(root, relative)
                    size = path.stat().st_size
                    if size <= 0 or ((kind == "video" or mime == 'image/gif') and size >= MAX_VIDEO_BYTES):
                        raise ValueError("File is empty or exceeds the video byte limit")
                    manifest.artifacts.append(
                        Attachment(
                            type=kind,
                            path=path.relative_to(root.resolve()).as_posix(),
                            size_bytes=size,
                            case_id=result.case_id,
                            media_type=mime,
                        )
                    )
                    if kind == "test":
                        accepted_test = relative
                except ValueError as exc:
                    manifest.omissions.append(f"{relative}: {exc}")
            manifest.tests.append(
                ManifestTest(
                    case_id=result.case_id,
                    runner=result.runner,
                    status=result.status,
                    detail=result.detail,
                    path=accepted_test,
                    rerun=result.rerun if accepted_test else [],
                    title=case.get("title", ""),
                    coverage=case.get("coverage", "requested_behavior"),
                    verification=case.get("verification", "live"),
                    interaction=case.get("interaction", "user"),
                    scripted_providers=(case.get("journey") or {}).get("scripted_providers", False),
                    checks=case.get("checks", []),
                    checkpoints=result.checkpoints,
                )
            )
            if accepted_test is None and manifest.status == "passed":
                manifest.status = "blocked"
                manifest.reason = "A generated test artifact is missing or invalid."
        for relative in ("tests/requirements.txt", "tests/README.md", "plan.json"):
            if (root / relative).is_file():
                try:
                    bundle_file(root, relative)
                    manifest.support_files.append(relative)
                except ValueError as exc:
                    manifest.omissions.append(str(exc))
        for receipt in sorted(root.glob('login-*/receipt.json')):
            relative = receipt.relative_to(root).as_posix()
            try:
                bundle_file(root, relative)
                manifest.support_files.append(relative)
            except ValueError as exc:
                manifest.omissions.append(str(exc))
        # Publish a journey, not a gallery of each checkpoint and retry. Full
        # execution receipts and files remain untouched in the local run folder.
        latest = {test.case_id: test for test in manifest.tests}
        manifest.tests = list(latest.values())
        selected = []
        for case_id, test in latest.items():
            files = [item for item in manifest.artifacts if item.case_id == case_id]
            selected.extend(item for item in files if item.type == 'test' and item.path == test.path)
            # Only use media from the final attempt, never a passing-looking GIF
            # from an earlier attempt when the final attempt failed to encode.
            result = latest_results[case_id]
            images = [item for item in files if item.type == 'screenshot' and item.path in result.screenshots]
            gifs = [item for item in images if item.media_type == 'image/gif']
            if gifs:
                selected.append(gifs[-1])
            elif images:
                selected.append(images[-1])
                manifest.omissions.append(f'{case_id}: GIF unavailable; one screenshot is attached instead.')
        # A reused local path may be listed in multiple attempts; attach it once.
        manifest.artifacts = list({item.path: item for item in selected}.values())
    temporary = root / "manifest.json.tmp"
    temporary.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(root / "manifest.json")
    return manifest
