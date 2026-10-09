"""Typed HTTP and terminal regression suites; every operation has explicit checks."""

import json
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, JsonValue, StrictInt, model_validator

from open_verify.action_spec import CommandArgs, RequestArgs
from open_verify.contracts import Contract


class TextCheck(Contract):
    mode: Literal['equals', 'contains'] = 'equals'
    value: str = Field(max_length=24000)

    @model_validator(mode='after')
    def nonempty_contains(self):
        """An empty substring cannot establish any useful assertion."""
        if self.mode == 'contains' and not self.value:
            raise ValueError('Contains requires a nonempty value')
        return self


class JSONCheck(Contract):
    field: list[str | Annotated[StrictInt, Field(ge=0)]] = Field(default_factory=list, max_length=30)
    value: JsonValue

    @model_validator(mode='after')
    def finite_json(self):
        """JSON expectations cannot contain NaN or infinities."""
        json.dumps(self.value, allow_nan=False)
        return self


class HTTPExpected(Contract):
    status: int = Field(ge=100, le=599, strict=True)
    headers: dict[str, str] = Field(default_factory=dict)
    text: TextCheck | None = None
    json_check: JSONCheck | None = None

    @model_validator(mode='after')
    def unique_headers(self):
        """Header names are case insensitive even when the input dictionary is not."""
        if len({k.lower() for k in self.headers}) != len(self.headers):
            raise ValueError('Expected header names must be unique ignoring case')
        return self


class CommandExpected(Contract):
    exit_code: StrictInt
    output: TextCheck | None = None


class HTTPTestStep(RequestArgs):
    kind: Literal['http']
    expect: HTTPExpected

    @model_validator(mode='after')
    def public_request(self):
        """Generated requests use public HTTP endpoints without embedded credentials."""
        url = urlsplit(self.url)
        if url.scheme not in {'http', 'https'} or not url.hostname or url.username is not None or url.password is not None:
            raise ValueError('Generated requests require an HTTP(S) URL without embedded credentials')
        if any(k.lower() in {'authorization', 'cookie', 'proxy-authorization'} for k in self.headers):
            raise ValueError('Authenticated request headers are not supported in generated suites')
        return self


class CommandTestStep(CommandArgs):
    kind: Literal['command']
    expect: CommandExpected


BackendStep = Annotated[HTTPTestStep | CommandTestStep, Field(discriminator='kind')]


class BackendTest(Contract):
    case_id: str = Field(min_length=1, max_length=160)
    interface: Literal['http', 'terminal']
    steps: list[BackendStep] = Field(min_length=1, max_length=30)
    timeout: float = Field(default=60, gt=0, le=300)
    checks: dict[str, list[Annotated[StrictInt, Field(ge=0)]]] = Field(default_factory=dict)

    @model_validator(mode='after')
    def typed_coverage(self):
        """Each planned case uses its own interface and references checked operations."""
        expected = 'http' if self.interface == 'http' else 'command'
        if any(s.kind != expected for s in self.steps):
            raise ValueError('All test steps must match the case interface')
        if any(not name.strip() or not indexes or any(i >= len(self.steps) for i in indexes)
               for name, indexes in self.checks.items()):
            raise ValueError('Completion checks must reference assertion-bearing steps')
        if len(self.model_dump_json()) > 100_000:
            raise ValueError('Generated test specification exceeds 100000 characters')
        return self
