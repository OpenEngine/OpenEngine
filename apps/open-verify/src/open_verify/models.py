"""Provider-independent contracts for plans, decisions, and evidence."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Case(Contract):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    interface: Literal["browser", "terminal", "http", "mixed"]
    prerequisites: list[str] = Field(default_factory=list)
    steps: list[str] = Field(min_length=1)
    expected: str = Field(min_length=1)


class Plan(Contract):
    project_summary: str
    startup: list[str]
    assumptions: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    cases: list[Case] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique_cases(self):
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("case IDs must be unique")
        return self


class Action(Contract):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=1)


class Finding(Contract):
    case_id: str
    status: Literal["passed", "failed", "blocked", "inconclusive"]
    actual: str = Field(min_length=1)
    evidence: list[str] = Field(default_factory=list)
    reproduction: list[str] = Field(default_factory=list)


class Question(Contract):
    text: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1, max_length=1)


class Impact(Contract):
    decision: Literal["verify", "skip", "uncertain"]
    reason: str = Field(min_length=1)
    material_ui_change: bool
    affected_files: list[str] = Field(default_factory=list)
    journeys: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent(self):
        if self.decision == "skip" and self.material_ui_change:
            raise ValueError("A material UI change cannot be skipped")
        if self.decision == "verify" and not self.journeys:
            raise ValueError("Verification needs at least one affected journey")
        return self


class Decision(Contract):
    kind: Literal["action", "impact", "plan", "finding", "question", "finish"]
    action: Action | None = None
    plan: Plan | None = None
    finding: Finding | None = None
    question: Question | None = None
    impact: Impact | None = None
    note: str = ""

    @model_validator(mode="after")
    def matching_payload(self):
        for field in ("action", "impact", "plan", "finding", "question"):
            if (getattr(self, field) is not None) != (self.kind == field):
                raise ValueError(f"{self.kind} decision has invalid {field} payload")
        return self
