"""Closed application-operation arguments shared by engines and generated suites."""

from typing import Literal

from pydantic import Field

from open_verify.contracts import Contract


class CommandArgs(Contract):
    argv: list[str] = Field(min_length=1)
    cwd: str = "."
    stdin: str = ""
    timeout: float = Field(default=30, gt=0, le=120)


class RequestArgs(Contract):
    url: str
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"] = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None
    timeout: float = Field(default=15, gt=0, le=60)


