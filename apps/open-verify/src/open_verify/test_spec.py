"""Declarative browser journeys shared by generation and runner adapters."""

from collections.abc import Callable
from typing import Annotated, Literal, Protocol

from pydantic import Field, model_validator

from open_verify.models import Contract


class Locator(Contract):
    by: Literal["role", "label", "text", "test_id"]
    name: str = Field(min_length=1)
    role: str | None = None

    @model_validator(mode="after")
    def role_required(self):
        if self.by == "role" and not self.role:
            raise ValueError("Role locators need a role")
        return self


class Click(Contract):
    kind: Literal["click"]
    locator: Locator


class Fill(Contract):
    kind: Literal["fill"]
    locator: Locator
    value: str


class Press(Contract):
    kind: Literal["press"]
    locator: Locator
    key: str


class ExpectText(Contract):
    kind: Literal["expect_text"]
    text: str = Field(min_length=1)
    visible: bool = True


class ExpectURL(Contract):
    kind: Literal["expect_url"]
    url: str = Field(min_length=1)


class Screenshot(Contract):
    kind: Literal["screenshot"]
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")


Step = Annotated[Click | Fill | Press | ExpectText | ExpectURL | Screenshot, Field(discriminator="kind")]


class BrowserTest(Contract):
    case_id: str = Field(min_length=1, max_length=160)
    url: str = Field(min_length=1)
    authenticated: bool = False
    steps: list[Step] = Field(min_length=1, max_length=40)
    timeout: float = Field(default=60, gt=0, le=120)

    @model_validator(mode="after")
    def assertion_required(self):
        if not any(isinstance(step, (ExpectText, ExpectURL)) for step in self.steps):
            raise ValueError("A regression test needs at least one explicit assertion")
        return self


class TestResult(Contract):
    case_id: str
    runner: str = "playwright"
    status: Literal["passed", "failed", "blocked"]
    detail: str
    test_file: str
    rerun: list[str]
    screenshots: list[str] = Field(default_factory=list)
    videos: list[str] = Field(default_factory=list)
    omissions: list[str] = Field(default_factory=list)


class BrowserRunner(Protocol):
    async def run(
        self,
        test: BrowserTest,
        *,
        capture_media: bool,
        on_result: Callable[[TestResult], None] | None = None,
    ) -> TestResult:
        """Checkpoint completed evidence before optional media work and update it afterward."""
        ...
