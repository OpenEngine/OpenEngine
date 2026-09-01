"""Deterministic evaluation primitives for scoping plans."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engine.domain import MilestoneScope, ScopingPlan, WorkOrder, WorkOrderStatus


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One versioned scoper input and its quality expectations."""

    name: str
    description: str
    milestones: tuple[MilestoneScope, ...]
    workorders: tuple[WorkOrder, ...]
    required_concepts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StructuralIssue:
    """A machine-checkable defect which must not be left to an LLM judge."""

    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class JudgeScore:
    """Structured output required from the quality judge."""

    chunk_size: int
    reviewability: int
    idempotency: int
    dependencies: int
    coverage: int
    minimality: int
    pass_: bool
    reasons: tuple[str, ...]

    @classmethod
    def from_json(cls, message: str) -> JudgeScore:
        value: Any = json.loads(message)
        dimensions = (
            "chunk_size", "reviewability", "idempotency", "dependencies",
            "coverage", "minimality",
        )
        if not isinstance(value, dict):
            raise ValueError("judge response must be a JSON object")
        if any(not isinstance(value.get(key), int) for key in dimensions):
            raise ValueError("judge dimensions must be integer scores")
        if any(not 1 <= value[key] <= 5 for key in dimensions):
            raise ValueError("judge dimensions must be scored from 1 to 5")
        reasons = value.get("reasons")
        if not isinstance(value.get("pass"), bool) or not isinstance(reasons, list):
            raise ValueError("judge response must contain pass and reasons")
        if not all(isinstance(reason, str) for reason in reasons):
            raise ValueError("judge reasons must be strings")
        return cls(*(value[key] for key in dimensions), value["pass"], tuple(reasons))

    @property
    def mean(self) -> float:
        return sum((self.chunk_size, self.reviewability, self.idempotency,
                    self.dependencies, self.coverage, self.minimality)) / 6


JUDGE_RUBRIC = """Score the proposed scoping plan from 1 (poor) to 5 (excellent) on:
- chunk_size: each work order is small and single-purpose;
- reviewability: each chunk has a concrete objective and inspectable evidence;
- idempotency: retrying or re-scoping will not duplicate work or effects;
- dependencies: prerequisites are necessary, valid, and explicit;
- coverage: milestone requirements and evidence are fully addressed;
- minimality: no redundant or unnecessary work is proposed.
A passing plan has no score below 3 and no structural issues. Return only JSON with
the six integer fields, a boolean `pass`, and a `reasons` list citing specific work.
"""


def validate_plan(case: EvalCase, plan: ScopingPlan) -> tuple[StructuralIssue, ...]:
    """Return all hard failures in ``plan`` for ``case``."""

    issues: list[StructuralIssue] = []
    milestone_ids = {item.milestone_id for item in case.milestones}
    existing = {item.workorder_id: item for item in case.workorders}
    active_signatures = {
        (item.spec.milestone_id, _norm(item.spec.name), _norm(item.spec.objective))
        for item in case.workorders
        if item.status is not WorkOrderStatus.CANCELLED
    }
    specs = [*plan.create, *(spec for item in plan.supersede for spec in item.replacements)]
    seen: set[tuple[object, str, str]] = set()
    for spec in specs:
        signature = (spec.milestone_id, _norm(spec.name), _norm(spec.objective))
        if spec.milestone_id not in milestone_ids:
            issues.append(StructuralIssue("unknown_milestone", spec.name))
        if signature in seen:
            issues.append(StructuralIssue("duplicate_proposal", spec.name))
        seen.add(signature)
        if signature in active_signatures:
            issues.append(StructuralIssue("recreates_existing_work", spec.name))
        for dependency in spec.dependencies:
            if dependency not in existing:
                issues.append(StructuralIssue("dangling_dependency", str(dependency)))
    targets = [*plan.cancel, *(item.workorder_id for item in plan.supersede)]
    for target in targets:
        if target not in existing:
            issues.append(StructuralIssue("unknown_existing_work", str(target)))
    if len(targets) != len(set(targets)):
        issues.append(StructuralIssue("duplicate_change", "work order changed more than once"))
    return tuple(issues)


def load_cases(path: Path) -> tuple[dict[str, Any], ...]:
    """Load raw versioned cases; domain conversion is owned by the live runner."""

    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("unsupported scoper eval dataset version")
    cases = value.get("cases")
    if not isinstance(cases, list) or not all(isinstance(case, dict) for case in cases):
        raise ValueError("scoper eval dataset cases must be objects")
    return tuple(cases)


def _norm(value: str) -> str:
    return " ".join(value.casefold().split()).rstrip(".")


__all__ = [
    "EvalCase", "JUDGE_RUBRIC", "JudgeScore", "StructuralIssue", "load_cases",
    "validate_plan",
]
