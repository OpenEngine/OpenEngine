"""Structured reviews and conservative selection, persisted in graph state."""

from collections.abc import Mapping
from dataclasses import dataclass
import json

from pydantic import BaseModel, ConfigDict, Field, field_validator

from engine.graph_runtime_langgraph.acp import ACPNode


class Finding(BaseModel):
    """A reviewer's evidence and runtime-owned lineage."""

    model_config = ConfigDict(extra="forbid", strict=True)
    id: str
    tagline: str
    description: str
    file: str = Field(min_length=1)
    line: int = Field(ge=1)
    evidence: str = Field(min_length=1)
    reviewer_node: str
    agent: str
    model: str
    facet: str
    reranker_node: str = ""
    retention_reason: str = ""
    reranker_agent: str = ""
    reranker_model: str = ""

    @field_validator("tagline", "description")
    @classmethod
    def concise_lines(cls, value, info):
        maximum = 2 if info.field_name == "tagline" else 3
        if not value.strip() or len(value.splitlines()) > maximum:
            raise ValueError(f"{info.field_name} must contain 1–{maximum} lines")
        return value


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tagline: str
    description: str
    file: str
    line: int
    evidence: str


class ReviewOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    findings: list[Candidate]


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str
    reason: str = Field(min_length=1)


class RerankOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    findings: list[Selection]


def structured(text: object, schema):
    """Require one JSON answer; accept a single surrounding Markdown fence."""
    value = str(text).strip()
    if value.startswith("```json\n") and value.endswith("\n```"):
        value = value[8:-4]
    return schema.model_validate_json(value)


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewNode(ACPNode):
    facet: str

    def _prompt(self, state):
        return (
            f"Review the implementation for {self.facet}. Inspect the changed code "
            "and surrounding code against the original task. Do not edit files, "
            "commit, publish, or fix anything. Only report actionable issues introduced "
            "by this change, supported by concrete evidence. Your acceptance criterion "
            "is a findings object, not prose. Return ONLY JSON matching "
            f"{json.dumps(ReviewOutput.model_json_schema())}. Use findings: [] if none. "
            "tagline: 1–2 lines explained to a layman; description: 1–3 followup lines "
            "explaining what happens and why it is bad. Include a file, positive line "
            "number and evidence.\nOriginal task:\n"
            f"{state.get('task', '')}\nImplementation:\n{state.get('implementation', '')}"
        )

    async def __call__(self, state: Mapping[str, object]) -> dict[str, object]:
        result = await ACPNode.__call__(self, state)
        parsed = structured(result[self.output_key], ReviewOutput)
        findings = [
            Finding(
                **item.model_dump(),
                id=f"{self.output_key}:{index}",
                reviewer_node=self.output_key,
                agent=self.agent,
                model=self.model,
                facet=self.facet,
            ).model_dump()
            for index, item in enumerate(parsed.findings, 1)
        ]
        return {self.output_key: findings}


@dataclass(frozen=True, slots=True, kw_only=True)
class RerankerNode(ACPNode):
    review_keys: tuple[str, ...]

    def _prompt(self, state):
        candidates = [item for key in self.review_keys for item in state[key]]
        return (
            "Rerank these findings. Inspect code to verify evidence without modifying "
            "anything. Aggressively squash noise: discard speculation, pre-existing "
            "issues, style preferences, negligible performance claims, and anything "
            "without a concrete consequence. Deduplicate overlapping issues across "
            "facets by retaining only the strongest representative. Keep only issues "
            "you can confidently defend as worth the author's attention. Do not invent "
            "or rewrite findings. Acceptance criterion: return ONLY JSON matching "
            f"{json.dumps(RerankOutput.model_json_schema())}, with retained candidate "
            "ids and a concrete reason each survived. Empty findings is valid.\n"
            f"Task: {state.get('task', '')}\nCandidates: {json.dumps(candidates)}"
        )

    async def __call__(self, state: Mapping[str, object]) -> dict[str, object]:
        result = await ACPNode.__call__(self, state)
        selected = structured(result[self.output_key], RerankOutput)
        candidates = {
            item["id"]: Finding.model_validate(item)
            for key in self.review_keys
            for item in state[key]
        }
        findings = []
        seen = set()
        for selection in selected.findings:
            if selection.id not in candidates or selection.id in seen:
                raise ValueError(f"Unknown or repeated finding: {selection.id}")
            seen.add(selection.id)
            finding = candidates[selection.id].model_copy(
                update={
                    "reranker_node": self.output_key,
                    "reranker_agent": self.agent,
                    "reranker_model": self.model,
                    "retention_reason": selection.reason,
                }
            )
            findings.append(finding.model_dump())
        return {self.output_key: findings}


def finding_comment(finding: Finding) -> str:
    return (
        f"### {finding.tagline}\n\n{finding.description}\n\n"
        f"Location: `{finding.file}:{finding.line}`\n\nEvidence: {finding.evidence}\n\n"
        f"Reviewer: {finding.agent} ({finding.model}), facet: {finding.facet}, "
        f"node: {finding.reviewer_node}.\n"
        f"Retained by {finding.reranker_node} ({finding.reranker_agent}, "
        f"{finding.reranker_model}): {finding.retention_reason}\n\n"
        f"<!-- engine-finding:{finding.id} -->"
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class PublishReviewNode(ACPNode):
    findings_key: str

    def _prompt(self, state):
        comments = [
            finding_comment(Finding.model_validate(item))
            for item in state[self.findings_key]
        ]
        return (
            "Publish the implementation in this checkout as a pull request. Create "
            "a descriptive agent/ branch, commit only the task changes with a "
            "Conventional Commit message, push that branch, and open a PR against "
            "main. Reuse the existing task PR if already published. Follow repository "
            "publishing instructions. Do not change implementation code. Do not add "
            "AI attribution to commits or the PR description. Then post each supplied "
            "comment body verbatim to that PR. Before posting, read existing comments "
            "and skip any whose engine-finding marker already exists, so retries do "
            "not duplicate comments. An empty list means no review comments. Your "
            "acceptance criterion is a published PR with all supplied comments attached; "
            'return ONLY JSON {"pr_url": "https://github.com/owner/repo/pull/123"}.\n'
            f"Task: {state.get('task', '')}\n"
            f"Implementation: {state.get('implementation', '')}\n"
            f"Comment bodies: {json.dumps(comments)}"
        )

    async def __call__(self, state):
        result = await ACPNode.__call__(self, state)
        parsed = structured(result[self.output_key], PublishedReview)
        return {"pr_url": parsed.pr_url}


class PublishedReview(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    pr_url: str = Field(pattern=r"^https://[^/]+/[^/]+/[^/]+/pull/[1-9][0-9]*$")
