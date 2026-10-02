"""Explicit action goals and independently evaluated assertions."""

from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from open_verify.contracts import Contract
from open_verify.test_spec import ExpectJSON, ExpectText, ExpectURL

Assertion = Annotated[ExpectText | ExpectURL | ExpectJSON, Field(discriminator="kind")]


class ActStep(Contract):
    kind: Literal["act"]
    instruction: str = Field(min_length=1, max_length=4000)
    timeout: float = Field(default=60, gt=0, le=300)
    max_actions: int = Field(default=12, ge=1, le=50)
    max_model_calls: int = Field(default=12, ge=1, le=50)


class AssertStep(Contract):
    kind: Literal["assert"]
    instruction: str = Field(min_length=1, max_length=4000)
    check: Assertion | None = Field(default=None, description="Exact assertion; omit for an independent judgment")
    mode: Literal["semantic", "visual"] = "semantic"
    timeout: float = Field(default=30, gt=0, le=120)
    max_model_calls: int = Field(default=2, ge=1, le=4)


    @model_validator(mode="after")
    def visual_requires_judge(self):
        """Pixels select a judge and cannot silently coexist with an exact check."""
        if self.mode == "visual" and self.check is not None:
            raise ValueError("A visual assertion cannot contain an exact check")
        return self


JourneyStep = Annotated[ActStep | AssertStep, Field(discriminator="kind")]


class BrowserJourney(Contract):
    url: str = Field(min_length=1)
    authenticated: bool = False
    steps: list[JourneyStep] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def ends_with_verification(self):
        if not isinstance(self.steps[-1], AssertStep):
            raise ValueError("A journey must end with an assertion, not the actor's verdict")
        return self


class StepAction(Contract):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=2000)


class ActDecision(Contract):
    kind: Literal["action", "complete"]
    action: StepAction | None = None
    status: Literal["passed", "failed", "blocked"] | None = None
    summary: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def matching_payload(self):
        if self.kind == "action":
            if self.action is None or self.status is not None or self.summary:
                raise ValueError("An action decision contains only its action")
        elif self.action is not None or self.status is None or not self.summary.strip():
            raise ValueError("A conclusion requires a status and summary, without an action")
        return self


class Judgment(Contract):
    explanation: str = Field(min_length=1, max_length=2000)
    verdict: Literal["holds", "fails", "inconclusive"]


class StepResult(Contract):
    index: int
    kind: Literal["act", "assert"]
    instruction: str
    status: Literal["passed", "failed", "blocked"]
    detail: str
    code: str | None = None
    cache: Literal["off", "miss", "hit", "stale", "bypass", "refresh"] = "off"
    cache_detail: str = ""
    model_calls: int = 0
    actions: int = 0
    evidence: list[str] = Field(default_factory=list)


class RunJourney(Contract):
    case_id: str
