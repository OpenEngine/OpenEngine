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
from dataclasses import dataclass, replace
from typing import Any

from engine.graph_runtime_langgraph.acp import ACPNode, TerminalEvent


@dataclass(frozen=True, slots=True)
class Finding:
    """One reviewer observation, structured for comment posting and reranking."""

    tagline: str
    """1-2 lines, as explained to a layman."""
    description: str
    """1-3 followup lines about what it is and why it's bad."""
    facet: str = ""
    """Which review facet produced this: security, bugs, performance, conciseness."""
    agent: str = ""
    """Which agent produced this: codex, claude."""
    file: str | None = None
    """The file this finding relates to, if any."""
    line: int | None = None
    """The line number, if applicable."""

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
        )

    def as_comment(self) -> str:
        """Format for posting as a PR comment, with lineage."""
        parts = [f"**{self.tagline}**", "", self.description]
        lineage: list[str] = []
        if self.agent:
            lineage.append(f"agent: {self.agent}")
        if self.facet:
            lineage.append(f"facet: {self.facet}")
        if lineage:
            parts.extend(["", f"_Produced by {', '.join(lineage)}_"])
        return "\n".join(parts)


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


#: The four review facets. Security uses a larger model; the rest use the
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
)


__all__ = [
    "Finding",
    "REVIEW_FACETS",
    "ReviewFacet",
    "ReviewNode",
    "RerankerNode",
]


def parse_findings(value: object) -> list[Finding]:
    """Reject malformed reviewer output instead of silently treating it as clean."""
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("findings must be a JSON array of objects")
    return [Finding.from_dict(item) for item in value]


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewNode(ACPNode):
    """Store validated findings under this facet's own key for parallel writes."""

    facet: str

    def _terminal_update(self, event: TerminalEvent) -> dict[str, object]:
        update = ACPNode._terminal_update(self, event)
        findings = parse_findings(update.get("findings"))
        return {
            self.output_key: [
                replace(finding, agent=self.agent, facet=self.facet).to_dict()
                for finding in findings
            ]
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class RerankerNode(ACPNode):
    """Keep the reranker's accepted findings as structured checkpoint state."""

    def _terminal_update(self, event: TerminalEvent) -> dict[str, object]:
        update = ACPNode._terminal_update(self, event)
        findings = parse_findings(update.get("findings"))
        if any(not finding.agent or not finding.facet for finding in findings):
            raise ValueError("reranked findings must retain reviewer lineage")
        return {self.output_key: [finding.to_dict() for finding in findings]}
