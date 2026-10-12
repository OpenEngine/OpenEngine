"""Review findings as first-class objects.

A *finding* is one thing a reviewer noticed. It has a tagline -- one or two
lines, written as though the reader has never seen the code -- and a
description: a few more lines about what it is and why it matters. Both are
short enough to be a PR comment rather than a report.

Findings flow through the graph as dicts (JSON-round-trippable through
LangGraph state) and are turned into `Finding` objects at the boundaries
where something needs to read one.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from typing import Any

from collections.abc import Mapping

from engine.domain import (
    TRIAGE_TOOL, ApprovalDecision, ApprovalKind, StepCompleted, WorkState, finding_comment,
)
from engine.graph_runtime_langgraph.acp import ACPNode, TerminalEvent
from engine.graph_runtime_langgraph.executions import current_execution, NoExecutionError
from engine.domain.findings_ledger import FindingStage, ReviewFinding, finding_id
from engine.runtime.findings import capture, triage


#: How bad a finding is, when a reviewer says; empty when it does not.
SEVERITIES = ("", "high", "medium", "low")


@dataclass(frozen=True, slots=True)
class Finding:
    """One reviewer observation, structured for comment posting and reranking."""

    tagline: str
    """1-2 lines, as explained to a layman."""
    description: str
    """1-3 followup lines about what it is and why it's bad."""
    facet: str = ""
    """Which review facet produced this: security, bugs, performance, conciseness, dryness."""
    agent: str = ""
    """Which agent produced this: codex, claude."""
    file: str | None = None
    """The file this finding relates to, if any."""
    line: int | None = None
    """The line number, if applicable."""
    severity: str = ""
    """`high`, `medium` or `low`, when the reviewer ranked it."""

    def __post_init__(self) -> None:
        for name, limit in (("tagline", 2), ("description", 3)):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value.splitlines()) > limit
            ):
                raise ValueError(f"{name} must contain 1-{limit} non-empty lines")
        if self.file is not None and not isinstance(self.file, str):
            raise ValueError("file must be a string")
        if self.line is not None and (type(self.line) is not int or self.line < 1):
            raise ValueError("line must be a positive integer")
        if not isinstance(self.agent, str) or not isinstance(self.facet, str):
            raise ValueError("agent and facet must be strings")
        if self.severity not in SEVERITIES:
            raise ValueError("severity must be high, medium or low")

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"tagline": self.tagline, "description": self.description}
        if self.facet:
            d["facet"] = self.facet
        if self.agent:
            d["agent"] = self.agent
        if self.file is not None:
            d["file"] = self.file
        if self.line is not None:
            d["line"] = self.line
        if self.severity:
            d["severity"] = self.severity
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Finding:
        return cls(
            tagline=data.get("tagline", ""),
            description=data.get("description", ""),
            facet=data.get("facet", ""),
            agent=data.get("agent", ""),
            file=data.get("file"),
            line=data.get("line"),
            severity=data.get("severity") or "",
        )

    def as_comment(self) -> str:
        """Format for posting as a PR comment, with lineage."""
        return finding_comment(self.tagline, self.description, agent=self.agent, facet=self.facet)


@dataclass(frozen=True, slots=True)
class ReviewFacet:
    """One angle a reviewer examines code from."""

    id: str
    """Short identifier used as node name suffix and state key."""
    name: str
    """Human-readable name for prompts and lineage."""
    focus: str
    """What the reviewer should look for, phrased as instructions."""
    elevated: bool = False
    """Whether this facet uses the elevated (larger) model tier."""


#: The review facets. Security uses a larger model; the rest use the
#: default tier.
REVIEW_FACETS: tuple[ReviewFacet, ...] = (
    ReviewFacet(
        id="security",
        name="Security",
        focus=(
            "Look for security vulnerabilities: injection flaws (SQL, command, "
            "XSS), authentication and authorization gaps, secrets or credentials "
            "in code, unsafe deserialization, path traversal, and any OWASP Top 10 "
            "issues. Evaluate whether inputs from external sources are validated "
            "and sanitized."
        ),
        elevated=True,
    ),
    ReviewFacet(
        id="bugs",
        name="Bugs & task adherence",
        focus=(
            "Look for correctness bugs, logic errors, off-by-one mistakes, null/"
            "None handling gaps, race conditions, and missing error handling. Also "
            "verify that the implementation actually does what the original task "
            "asked for -- flag anything the task requested that is missing, and "
            "anything present that the task did not ask for."
        ),
    ),
    ReviewFacet(
        id="performance",
        name="Performance",
        focus=(
            "Look for performance issues: unnecessary allocations, O(n^2) or worse "
            "algorithms where better ones are straightforward, missing indexes on "
            "queries, unbounded loops, blocking calls in async contexts, and "
            "resource leaks (open files, connections, memory)."
        ),
    ),
    ReviewFacet(
        id="conciseness",
        name="Conciseness",
        focus=(
            "Look for unnecessary complexity: dead code, redundant conditions, "
            "over-abstraction, copy-paste duplication that should be a shared "
            "function, overly verbose patterns that have a simpler equivalent in "
            "the language or framework, and any code that could be removed without "
            "changing behavior."
        ),
    ),
    ReviewFacet(
        id="dryness",
        name="DRYness & code duplication",
        focus=(
            "Check the changes for DRY (Don't Repeat Yourself) violations and code "
            "duplication. Search the repository beyond the changed files for "
            "existing helpers, utilities, and equivalent logic that the change "
            "could reuse. Look for duplicated business rules and repeated logic "
            "introduced by the change, both within the diff and against existing "
            "code. Report actionable duplication with the changed location and "
            "the existing equivalent's path, explaining what should be reused or "
            "consolidated and why. Do not flag unrelated pre-existing duplication "
            "or recommend abstractions for merely similar code with different "
            "responsibilities."
        ),
    ),
)


__all__ = [
    "Finding",
    "REVIEW_FACETS",
    "ReviewFacet",
    "SEVERITIES",
    "ReviewNode",
    "RerankerNode",
    "TRIAGE_TOOL",
    "TriageNode",
]


def parse_findings(value: object, *, require_lineage: bool = False) -> list[Finding]:
    """Reject malformed reviewer output instead of silently treating it as clean."""
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("findings must be a JSON array of objects")
    findings = [Finding.from_dict(item) for item in value]
    if require_lineage and any(not finding.agent or not finding.facet for finding in findings):
        raise ValueError("reranked findings must retain reviewer lineage")
    return findings


def _capture_stage(stage: FindingStage, findings: list[Finding]) -> None:
    try:
        execution = current_execution()
        ledger = getattr(execution.runtime, "findings_ledger", None)
        if ledger is None:
            return
        rows = [
            ReviewFinding(run_id=str(execution.run_id), node_id=str(execution.node_id),
                          stage=stage, **finding.to_dict())
            for finding in findings
        ]
        capture(ledger, rows)
    except NoExecutionError:
        pass  # Nodes may also be invoked without a runtime.
    except Exception:
        logging.getLogger(__name__).exception("Could not capture %s findings", stage)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewNode(ACPNode):
    """Store validated findings under this facet's own key for parallel writes."""

    facet: str
    graph_node_group: str = WorkState.REVIEW

    @property
    def graph_node_findings_key(self) -> str:
        """Where this reviewer's findings are kept in run state."""
        return self.output_key

    @staticmethod
    def validate_completion(event: StepCompleted) -> None:
        outputs = {output.name: output.value for output in event.outputs}
        parse_findings(outputs.get("findings"))

    def _terminal_update(self, event: TerminalEvent) -> dict[str, object]:
        update = ACPNode._terminal_update(self, event)
        findings = parse_findings(update.get("findings"))
        findings = [replace(finding, agent=self.agent, facet=self.facet) for finding in findings]
        _capture_stage("raw", findings)
        return {self.output_key: [finding.to_dict() for finding in findings]}


@dataclass(frozen=True, slots=True, kw_only=True)
class RerankerNode(ACPNode):
    """Keep the reranker's accepted findings as structured checkpoint state."""

    graph_node_group: str = WorkState.REVIEW

    @property
    def graph_node_findings_key(self) -> str:
        """Where the findings that survived reranking are kept in run state."""
        return self.output_key

    @staticmethod
    def validate_completion(event: StepCompleted) -> None:
        outputs = {output.name: output.value for output in event.outputs}
        parse_findings(outputs.get("findings"), require_lineage=True)

    def _terminal_update(self, event: TerminalEvent) -> dict[str, object]:
        update = ACPNode._terminal_update(self, event)
        findings = parse_findings(update.get("findings"), require_lineage=True)
        _capture_stage("reranked", findings)
        return {self.output_key: [finding.to_dict() for finding in findings]}


@dataclass(frozen=True, slots=True, kw_only=True)
class TriageNode:
    """Wait for a person to choose which surviving findings get fixed.

    The choice arrives the way a human review's note does: as steering sent
    before the decision, here a JSON array of the chosen findings. Accepting
    with nothing sent chooses them all; cancelling chooses none and lets the
    run finish, rather than failing it -- a review nobody wants fixed is a
    review that is done.

        POST .../steering    [{"tagline": ..., "description": ..., ...}]
        POST .../approvals   accept
    """

    findings_key: str
    """Where the findings to choose from are kept in run state."""
    output_key: str = "fix"
    """Where the chosen findings land, for the fixing node to read."""
    graph_node_name: str = "Triage"
    graph_node_kind: str = "human"
    graph_node_description: str = "A person chooses which findings to fix."
    graph_node_group: str = WorkState.REVIEW
    graph_node_show_in_sidebar: bool = False

    @property
    def graph_node_findings_key(self) -> str:
        """The findings a person is choosing from, for a client to show."""
        return self.findings_key

    async def __call__(self, state: Mapping[str, object]) -> dict[str, object]:
        execution = current_execution()
        await execution.say("The review is done. Choose the findings to fix, or finish.")
        decision = await execution.ask(
            reason="a choice of findings to fix",
            kind=ApprovalKind.USER_INPUT,
            tool_name=TRIAGE_TOOL,
            cancel_run=False,
        )
        note = "\n".join(execution.pending_messages()).strip()
        if decision is ApprovalDecision.CANCEL:
            chosen: list[Finding] = []
        elif note:
            chosen = parse_findings(note)
        else:
            chosen = parse_findings(state.get(self.findings_key) or [])
        try:
            ledger = getattr(execution.runtime, "findings_ledger", None)
            rows = [
                ReviewFinding(run_id=str(execution.run_id), node_id=str(execution.node_id),
                              stage="reranked", **finding.to_dict())
                for finding in parse_findings(state.get(self.findings_key) or [])
            ]
            selected = {finding_id(str(execution.run_id), "reranked", f.file, f.line, f.tagline) for f in chosen}
            triage(ledger, rows, selected)
        except Exception:
            logging.getLogger(__name__).exception("Could not capture triage outcomes")
        await execution.say(
            f"Fixing {len(chosen)} finding{'s' if len(chosen) != 1 else ''}."
            if chosen else "Finished without fixing anything."
        )
        return {self.output_key: [finding.to_dict() for finding in chosen]}
