"""Short, cached probes of the provider and model an actual session will use."""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass, fields, is_dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from collections.abc import Mapping

from langgraph_acp.agent import ACPAgentProvider
from langgraph_acp._json import JSONValue
from langgraph_acp.events import ACPEventType


@dataclass(frozen=True)
class AgentReadiness:
    ready: bool
    reason: str = ""


def missing_runtime(provider: ACPAgentProvider) -> str | None:
    """Do not download adapters for absent built-in CLIs during auto-selection.

    Custom adapter commands and explicitly selected adapter-only setups are
    still probed normally. Codex discovery honors the same CODEX_PATH as launch.
    """
    from langgraph_acp.providers import ClaudeACPProvider, CodexACPProvider, OpenCodeACPProvider
    from langgraph_acp.providers.claude import CLAUDE_ACP_COMMAND
    from langgraph_acp.providers.codex import CODEX_ACP_COMMAND
    from langgraph_acp.providers.opencode import OPENCODE_ACP_COMMAND

    env = {**os.environ, **(getattr(provider, "env", None) or {})}
    command = getattr(provider, "command", ())
    binary = None
    if isinstance(provider, CodexACPProvider) and tuple(command) == CODEX_ACP_COMMAND:
        binary = env.get("CODEX_PATH") or "codex"
    elif isinstance(provider, ClaudeACPProvider) and tuple(command) == CLAUDE_ACP_COMMAND:
        binary = "claude"
    elif isinstance(provider, OpenCodeACPProvider) and tuple(command) == OPENCODE_ACP_COMMAND:
        binary = "opencode"
    if binary and shutil.which(binary, path=env.get("PATH")) is None:
        return f"{binary} is not installed on this backend"
    return None


def _config_stamp(provider: ACPAgentProvider) -> tuple[tuple[str, int], ...]:
    env = {**os.environ, **(getattr(provider, "env", None) or {})}
    paths = [
        Path(env.get("CODEX_HOME") or Path.home() / ".codex") / name
        for name in ("config.toml", "auth.json")
    ]
    paths += [Path(env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"]
    paths += [Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "opencode/opencode.json"]
    stamp = []
    for path in paths:
        try:
            stamp.append((str(path), path.stat().st_mtime_ns))
        except OSError:
            stamp.append((str(path), 0))
    return tuple(stamp)


class AgentReadinessCache:
    """One probe per effective configuration, shared by simultaneous submissions.

    Probes use a temporary directory and no MCP tools. Permission callbacks
    belonging to a graph execution must not be invoked by a preflight session.
    No credentials are read; config/auth file timestamps only invalidate cache.
    """

    def __init__(self, *, ttl: float = 60, timeout: float = 30) -> None:
        self.ttl, self.timeout = ttl, timeout
        self._results: dict[object, tuple[float, AgentReadiness]] = {}
        self._pending: dict[object, asyncio.Task[AgentReadiness]] = {}

    async def aclose(self) -> None:
        tasks = tuple(self._pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._pending.clear()
        self._results.clear()

    async def check(
        self, provider: ACPAgentProvider,
        session_config: Mapping[str, JSONValue] | None = None,
    ) -> AgentReadiness:
        key = (id(provider), repr(session_config), _config_stamp(provider))
        cached = self._results.get(key)
        if cached and cached[0] > monotonic():
            return cached[1]
        task = self._pending.get(key)
        if task is None:
            task = asyncio.create_task(self._probe(provider, session_config))
            self._pending[key] = task
        try:
            result = await asyncio.shield(task)
            self._results[key] = (monotonic() + self.ttl, result)
            return result
        finally:
            if task.done():
                self._pending.pop(key, None)
            now = monotonic()
            self._results = {k: v for k, v in self._results.items() if v[0] > now}

    async def _probe(
        self, provider: ACPAgentProvider,
        config: Mapping[str, JSONValue] | None,
    ) -> AgentReadiness:
        try:
            if is_dataclass(provider) and not isinstance(provider, type):
                names = {item.name for item in fields(provider)}
                provider = replace(provider, **{name: None for name in ("permissions", "elicitations") if name in names})
            async with asyncio.timeout(self.timeout):
                client = await provider.connect()
                try:
                    with TemporaryDirectory(prefix="engine-agent-probe-") as cwd:
                        session = await client.new_session(cwd=cwd, session_config=config)
                        try:
                            completed = False
                            async for event in session.prompt("Reply OK only. Do not use tools or access files."):
                                if event.type == ACPEventType.PROMPT_COMPLETED:
                                    completed = event.data.get("stopReason") == "end_turn"
                            return AgentReadiness(completed, "" if completed else "agent did not complete its readiness probe")
                        finally:
                            await session.close()
                finally:
                    await client.close()
        except TimeoutError:
            return AgentReadiness(False, "adapter readiness probe timed out")
        except Exception as error:  # noqa: BLE001 -- provider boundary: readiness retains the actual diagnostic
            from langgraph_acp.providers import CodexACPProvider
            reason = str(error)
            if isinstance(provider, CodexACPProvider):
                reason += f". Runtime: {await provider.runtime_description()}"
            return AgentReadiness(False, reason)
