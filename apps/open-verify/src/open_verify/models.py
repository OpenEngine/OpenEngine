"""Provider-independent contracts for plans, decisions, and evidence."""

from typing import Any, Literal

from pydantic import Field, model_validator

from open_verify.contracts import Contract
from open_verify.journey_spec import BrowserJourney


class Case(Contract):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    interface: Literal["browser", "terminal", "http", "mixed"]
    verification: Literal["live", "existing_tests"] = "live"
    interaction: Literal["user", "library"] = Field(default="user",
        description="user: real application UI/API/CLI actions; library: supporting in-process API checks")
    coverage: Literal["changed_behavior", "regression", "requested_behavior"] = "requested_behavior"
    journey: BrowserJourney | None = None
    prerequisites: list[str] = Field(default_factory=list)
    steps: list[str] = Field(min_length=1)
    expected: str = Field(min_length=1)
    checks: list[str] = Field(default_factory=list, max_length=10,
        description="Concrete completion checks for this journey; every check must have a test assertion")


    @model_validator(mode="after")
    def journey_coverage(self):
        """Reject unusable structured cases before setup or application actions."""
        if self.interaction == 'library' and self.interface != 'terminal':
            raise ValueError('Direct library checks require the terminal interface')
        if self.journey is not None:
            if self.interface != "browser" or len(self.id) > 160:
                raise ValueError("A structured journey requires a browser case ID of at most 160 characters")
            assertions = [s.instruction for s in self.journey.steps if s.kind == "assert"]
            if len(assertions) > 10 or len(set(assertions)) != len(assertions) or any(not s.strip() for s in assertions):
                raise ValueError("A journey needs at most 10 unique, nonempty assertion instructions")
            if self.checks and set(self.checks) != set(assertions):
                raise ValueError("Journey assertions must cover the planned checks verbatim")
        return self


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
