"""Explicit action goals and independently evaluated assertions."""

from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from open_verify.action_spec import CommandArgs
from open_verify.contracts import Contract
from open_verify.test_spec import ExpectJSON, ExpectSameURL, ExpectText, ExpectURL

Assertion = Annotated[ExpectText | ExpectURL | ExpectJSON | ExpectSameURL, Field(discriminator="kind")]


class ActStep(Contract):
    kind: Literal["act"]
    instruction: str = Field(min_length=1, max_length=4000)
    timeout: float = Field(default=60, gt=0, le=300)
    max_actions: int = Field(default=12, ge=1, le=50)
    max_model_calls: int = Field(default=12, ge=1, le=50,
        description="Actor provider requests including repair; host goal verification has a separate bounded allowance")


class HTTPObservation(Contract):
    """A declared read-only HTTP evidence source, collected by the host for a judgment."""

    url: str = Field(min_length=1)
    timeout: float = Field(default=10, gt=0, le=30)

    @model_validator(mode='after')
    def public_http_source(self):
        """Allow same-application relative paths or explicit HTTP(S) sources without credentials."""
        parsed = urlsplit(self.url)
        relative = (self.url.startswith('/') and not self.url.startswith('//')
                    and not parsed.scheme and not parsed.netloc and '\\' not in self.url)
        absolute = (parsed.scheme in {'http', 'https'} and parsed.hostname
                    and parsed.username is None and parsed.password is None and '\\' not in self.url)
        if not (relative or absolute) or parsed.fragment:
            raise ValueError('HTTP evidence requires HTTP(S) without credentials or a root-relative path')
        _ = parsed.port
        return self


class AssertStep(Contract):
    kind: Literal["assert"]
    instruction: str = Field(min_length=1, max_length=4000)
    check: Assertion | None = Field(default=None, description="Exact assertion; omit for an independent judgment")
    mode: Literal["semantic", "visual"] = "semantic"
    timeout: float = Field(default=30, gt=0, le=120)
    max_model_calls: int = Field(default=2, ge=1, le=4)
    remember_as: str | None = Field(default=None, min_length=1, max_length=80)
    evidence_requests: list[HTTPObservation] = Field(default_factory=list, max_length=3,
        description="Fresh host GET evidence for a semantic assertion; no actor summaries or credentials")
    compare_to: str | None = Field(default=None, min_length=1, max_length=80,
        description='Earlier host-captured textual observation for a semantic before/after comparison')


    @model_validator(mode="after")
    def visual_requires_judge(self):
        """Pixels select a judge and cannot silently coexist with an exact check."""
        if self.evidence_requests and (self.check is not None or self.mode != 'semantic'):
            raise ValueError('Additional HTTP evidence requires a semantic judgment without an exact check')
        if self.mode == "visual" and self.check is not None:
            raise ValueError("A visual assertion cannot contain an exact check")
        if self.compare_to is not None and (self.check is not None or self.mode == 'visual'):
            raise ValueError('compare_to requires a semantic judgment without an exact check')
        return self


JourneyStep = Annotated[ActStep | AssertStep, Field(discriminator="kind")]


class SetupProbe(CommandArgs):
    """A fixed setup contract probe, separate from product completion checks."""

    instruction: str = Field(min_length=1, max_length=2000)


class JourneyEntry(Contract):
    """Evidence for journey.url and controls required before the first product action."""

    evidence: list[str] = Field(min_length=1, max_length=5)
    controls: list[AssertStep] = Field(min_length=1, max_length=5)

    @model_validator(mode='after')
    def no_journey_baselines(self):
        if any(c.remember_as or c.compare_to or isinstance(c.check, ExpectSameURL) for c in self.controls):
            raise ValueError('Entry controls cannot reference journey baselines')
        return self


class RepairJourneyEntry(Contract):
    case_id: str
    url: str = Field(min_length=1)
    entry: JourneyEntry
    reason: str = Field(min_length=1, max_length=1500)


class BrowserJourney(Contract):
    entry: JourneyEntry | None = None
    url: str = Field(min_length=1)
    authenticated: bool = False
    steps: list[JourneyStep] = Field(min_length=1, max_length=20)
    scripted_providers: bool = Field(default=False,
        description="True when the QA environment substitutes scripted model/provider responses")
    setup_probes: list[SetupProbe] = Field(default_factory=list, max_length=3,
        description="Fixed commands that validate fixture contracts before browser actions; exit 0 required")
    readiness: list[AssertStep] = Field(default_factory=list, max_length=5,
        description="Independent app-visible setup checks, run before any journey action; required for provisioned fixtures")

    @model_validator(mode="after")
    def ends_with_verification(self):
        if self.scripted_providers and not self.setup_probes:
            raise ValueError('Scripted providers require setup_probes validating prompt selection and terminal responses')
        parsed = urlsplit(self.url)
        relative = (self.url.startswith('/') and not self.url.startswith('//')
                    and not parsed.scheme and not parsed.netloc and '\\' not in self.url)
        absolute = (parsed.scheme in {'http', 'https'} and parsed.hostname
                    and parsed.username is None and parsed.password is None)
        if not (relative or absolute):
            raise ValueError('Journey URL must be an absolute HTTP(S) URL without credentials or a root-relative path')
        if not isinstance(self.steps[-1], AssertStep):
            raise ValueError("A journey must end with an assertion, not the actor's verdict")
        remembered = set()
        for step in self.steps:
            if isinstance(step, AssertStep):
                reference = step.compare_to or (step.check.baseline if isinstance(step.check, ExpectSameURL) else None)
                if reference is not None and reference not in remembered:
                    raise ValueError('Comparison requires an earlier remembered assertion observation')
                if step.remember_as:
                    if step.remember_as in remembered:
                        raise ValueError('Observation baseline names must be unique')
                    remembered.add(step.remember_as)
        if any(s.remember_as or s.compare_to or isinstance(s.check, ExpectSameURL) for s in self.readiness):
            raise ValueError('Readiness assertions cannot use journey baselines')
        return self


class StepAction(Contract):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=2000)


class ActDecision(Contract):
    kind: Literal["action", "complete"]
    action: StepAction | None = None
    outcome: Literal["done", "blocked"] | None = None
    summary: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def matching_payload(self):
        if self.kind == "action":
            if self.action is None or self.outcome is not None or self.summary:
                raise ValueError("An action decision contains only its action")
        elif self.action is not None or self.outcome is None or not self.summary.strip():
            raise ValueError("An action completion requires an outcome and summary, without an action")
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


class HealthJudgment(Judgment):
    """Diagnose observed QA failures without deciding whether the PR caused them."""

    diagnosis: Literal['fixture', 'assertion', 'action', 'application', 'unknown'] = 'unknown'


class RunJourney(Contract):
    case_id: str
    base_url: str | None = Field(default=None,
        description='Discovered HTTP(S) application origin for a planned root-relative journey URL')

    @model_validator(mode='after')
    def application_origin(self):
        if self.base_url is not None:
            parsed = urlsplit(self.base_url)
            if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.path not in {'', '/'} or parsed.query or parsed.fragment
                    or '\\' in self.base_url):
                raise ValueError('base_url must be an HTTP(S) origin without credentials, path, query or fragment')
            _ = parsed.port  # Reject malformed port values before execution.
        return self
