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
from open_verify.test_spec import TestResult


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
    for package in ("open-verify", "playwright", "langgraph", "langgraph-acp"):
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
    )
    if manifest.status not in {"skipped", "planned"}:
        for result in results:
            manifest.omissions.extend(result.omissions)
            attachments = [(result.test_file, "test", "text/x-python")]
            if (report.get("impact") or {}).get("material_ui_change"):
                attachments.extend((p, "screenshot", "image/png") for p in result.screenshots)
                attachments.extend((p, "video", "video/mp4") for p in result.videos)
            accepted_test = None
            for relative, kind, mime in attachments:
                try:
                    path = bundle_file(root, relative)
                    size = path.stat().st_size
                    if size <= 0 or (kind == "video" and size >= MAX_VIDEO_BYTES):
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
    temporary = root / "manifest.json.tmp"
    temporary.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(root / "manifest.json")
    return manifest
