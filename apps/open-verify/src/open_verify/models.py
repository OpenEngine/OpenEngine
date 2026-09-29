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


class Decision(Contract):
    kind: Literal["action", "plan", "finding", "question", "finish"]
    action: Action | None = None
    plan: Plan | None = None
    finding: Finding | None = None
    question: Question | None = None
    note: str = ""

    @model_validator(mode="after")
    def matching_payload(self):
        for field in ("action", "plan", "finding", "question"):
            if (getattr(self, field) is not None) != (self.kind == field):
                raise ValueError(f"{self.kind} decision has invalid {field} payload")
        return self
