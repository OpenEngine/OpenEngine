"""Small synchronous ledger: terminal graph updates are synchronous callbacks."""
from collections.abc import Sequence
from typing import Protocol

from engine.domain.findings_ledger import ReviewFinding


class FindingsLedger(Protocol):
    def record_findings(self, findings: Sequence[ReviewFinding]) -> None: ...

    def attach_comment(self, finding_id: str, comment_id: int, *, pr_url: str, head_sha: str | None) -> None: ...

    def record_outcome(self, finding_id: str, signal: str, *, source: str, value: str | None = None) -> None: ...

    def list_findings(self, *, pr_url: str | None = None, run_id: str | None = None) -> Sequence[ReviewFinding]: ...
