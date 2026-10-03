"""Model-free application engine contract and checked tool dispatch."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from open_verify.artifacts import Artifacts
from open_verify.models import Contract

READ_TOOLS = frozenset({"list_files", "read_file", "read_change_diff"})
STAGES = frozenset({"discover", "execute"})


class ActionError(ValueError):
    """A checked engine refusal with a machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class Authentication(Protocol):
    """Engine-owned login service; credentials and session state stay outside prompts."""

    state: dict | None

    async def run(self, request, *, capture_media: bool) -> dict: ...


class Engine(Protocol):
    """The runner uses capabilities and receipts, never provider-specific handles."""

    project: Path
    allow_exec: bool

    def catalog(self, stage: str) -> dict: ...

    async def execute(self, name: str, arguments: dict, *, stage: str = "execute") -> dict: ...

    def environment(self) -> dict:
        """Return public setup context without leaking live process or browser objects."""
        ...

    def check_url(self, url: str) -> None: ...

    def create_authentication(self, *, progress: Callable[[str], None]) -> Authentication: ...

    async def open_journey(self, *, url: str, authenticated: bool, authentication) -> "Engine": ...

    async def assert_check(self, check) -> dict: ...

    async def close(self) -> list[str]: ...


@dataclass(frozen=True)
class ToolSpec:
    """A tool's schema, allowed phases and implementation are declared together."""

    arguments: type[Contract]
    description: str
    execute: Callable[..., Awaitable[dict]]
    stages: frozenset[str] = frozenset({"execute"})


class ActionDispatcher:
    """Validate and authorize a tool before invoking it; record every accepted or refused call."""

    def __init__(self, tools: Mapping[str, ToolSpec], artifacts: Artifacts):
        self.tools = dict(tools)
        self.artifacts = artifacts

    def catalog(self, stage: str) -> dict:
        """Only advertise tools that this phase may execute."""
        if stage not in STAGES:
            raise ValueError(f"Unknown execution stage: {stage}")
        return {
            name: {"description": tool.description, "arguments": tool.arguments.model_json_schema()}
            for name, tool in self.tools.items() if stage in tool.stages
        }

    async def execute(self, name: str, arguments: dict, *, stage: str = "execute") -> dict:
        """Return an evidence receipt; cancellation propagates to the runner."""
        try:
            if stage not in STAGES:
                raise ValueError(f"Unknown execution stage: {stage}")
            tool = self.tools.get(name)
            if tool is None:
                raise ValueError(f"Unknown tool: {name}")
            if stage not in tool.stages:
                raise ValueError(f"Tool {name} is not allowed during {stage}")
            args = tool.arguments.model_validate(arguments)
            result = await tool.execute(args)
            return self.artifacts.record(name, arguments, result, True)
        except Exception as exc:
            return self.artifacts.record(name, arguments, {"error": str(exc),
                **({"code": exc.code} if isinstance(exc, ActionError) else {})}, False)
