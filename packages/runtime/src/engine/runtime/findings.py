"""Best-effort capture and deterministic merge observations."""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from difflib import SequenceMatcher
import logging
import asyncio

from engine.domain.findings_ledger import ReviewFinding
from engine.ports.findings_ledger import FindingsLedger
from engine.runtime.change_requests import change_request, pull_request_url

log = logging.getLogger(__name__)


def canonical_pr_url(url: str) -> str:
    request = change_request(url)
    if request and request.kind == "pull":
        return pull_request_url(request.project, request.number)
    return url


def capture(ledger: FindingsLedger | None, findings: Sequence[ReviewFinding]) -> bool:
    if ledger is None:
        return False
    try:
        findings = [
            replace(finding, pr_url=canonical_pr_url(finding.pr_url)) if finding.pr_url else finding
            for finding in findings
        ]
        ledger.record_findings(findings)
        for finding in findings:
            if finding.stage == "posted" and finding.comment_id is not None and finding.pr_url:
                ledger.attach_comment(
                    finding.id, finding.comment_id, pr_url=finding.pr_url, head_sha=finding.head_sha,
                )
                ledger.record_outcome(
                    finding.id, "comment_posted", source="github", value=str(finding.comment_id),
                )
        return True
    except Exception:
        log.exception("Could not capture review findings")
        return False


def triage(
    ledger: FindingsLedger | None, findings: Sequence[ReviewFinding], selected: set[str],
) -> None:
    if ledger is None:
        return
    try:
        ledger.record_findings(findings)
        for finding in findings:
            ledger.record_outcome(
                finding.id, "triage_selected" if finding.id in selected else "triage_rejected", source="triage",
            )
        ledger.record_findings([replace(finding, stage="triaged") for finding in findings if finding.id in selected])
    except Exception:
        log.exception("Could not capture finding triage")


def changed_near_line(before: str, after: str, line: int) -> bool:
    """Compare in the reviewed file's coordinates, including insertion boundaries."""
    low, high = max(1, line - 3), line + 3
    matcher = SequenceMatcher(None, before.splitlines(), after.splitlines(), autojunk=False)
    for tag, start, end, _, _ in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert":
            if low - 1 <= start <= high:
                return True
        elif start + 1 <= high and end >= low:
            return True
    return False


async def reconcile(
    ledger: FindingsLedger, pr_url: str, merge_sha: str,
    read_file: Callable[[str, str], Awaitable[str]],
) -> int:
    """Record merge signals; missing/unreadable files must raise, never imply unchanged.

    A caller may return an empty string for a confirmed deleted file. Reading
    full blobs avoids truncated patches and uses head, not merge-base, coordinates.
    """
    count = 0
    files: dict[tuple[str, str], str] = {}
    for finding in ledger.list_findings(pr_url=canonical_pr_url(pr_url)):
        if finding.stage != "posted" or not finding.file or not finding.line or not finding.head_sha:
            continue
        try:
            for sha in (finding.head_sha, merge_sha):
                key = (sha, finding.file)
                if key not in files:
                    files[key] = await read_file(sha, finding.file)
            before, after = files[(finding.head_sha, finding.file)], files[(merge_sha, finding.file)]
            if finding.line > len(before.splitlines()):
                raise ValueError("Finding line does not exist at the reviewed head")
            changed = await asyncio.to_thread(changed_near_line, before, after, finding.line)
            ledger.record_outcome(
                finding.id, "fixed_before_merge" if changed else "unchanged_at_merge",
                source="merge_diff", value=merge_sha,
            )
            count += 1
        except Exception:
            log.exception("Could not reconcile finding %s", finding.id)
    return count
