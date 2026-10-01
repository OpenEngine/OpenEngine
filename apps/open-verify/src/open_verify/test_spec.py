"""Declarative browser journeys shared by generation and runner adapters."""

from collections.abc import Callable
from typing import Annotated, Literal, Protocol

from pydantic import Field, JsonValue, model_validator

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

    @model_validator(mode="after")
    def interactive_role(self):
        if self.locator.by == "role" and self.locator.role not in {
            "button", "link", "checkbox", "radio", "tab", "menuitem",
            "menuitemcheckbox", "menuitemradio", "option", "switch", "combobox",
        }:
            raise ValueError("Click an interactive control, not a group or container")
        return self


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


class Reload(Contract):
    kind: Literal["reload"]


class Navigate(Contract):
    kind: Literal["navigate"]
    path: str = Field(pattern=r"^/([^/\s\\][^\s\\]*)?$")


class ExpectJSON(Contract):
    kind: Literal["expect_json"]
    path: str = Field(pattern=r"^/([^/\s\\][^\s\\]*)?$")
    status: int = Field(default=200, ge=100, le=599)
    field: list[str | int] = Field(default_factory=list)
    value: JsonValue


Step = Annotated[Click | Fill | Press | ExpectText | ExpectURL | Screenshot | Reload | Navigate | ExpectJSON, Field(discriminator="kind")]


class BrowserTest(Contract):
    case_id: str = Field(min_length=1, max_length=160)
    url: str = Field(min_length=1)
    authenticated: bool = False
    steps: list[Step] = Field(min_length=1, max_length=40)
    timeout: float = Field(default=60, gt=0, le=120)
    checks: dict[str, list[int]] = Field(default_factory=dict,
        description="Map each planned completion check verbatim to zero-based assertion step indexes")
    retry_reason: str = Field(default="", max_length=2000,
        description="For a retry, diagnose the prior failure and explain the correction without weakening coverage")

    @model_validator(mode="after")
    def assertion_required(self):
        assertions = (ExpectText, ExpectURL, ExpectJSON)
        if not any(isinstance(step, assertions) for step in self.steps):
            raise ValueError("A regression test needs at least one explicit assertion")
        for indexes in self.checks.values():
            if not indexes or any(i < 0 or i >= len(self.steps) or not isinstance(self.steps[i], assertions) for i in indexes):
                raise ValueError("Completion checks must reference assertion steps")
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
