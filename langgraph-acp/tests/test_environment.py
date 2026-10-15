"""Assert the actual child environment, including absent explicit env."""

import asyncio
import json
from pathlib import Path
import sys

import pytest

from langgraph_acp import HOST_ENV_ALLOWLIST, StdioACPProvider
from langgraph_acp.environment import agent_environment


@pytest.mark.parametrize("explicit", [None, {"EXPLICIT_KEY": "selected"}])
def test_child_only_receives_approved_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: dict[str, str] | None) -> None:
    report = tmp_path / "environment.json"
    monkeypatch.setenv("HOST_SECRET", "must-not-inherit")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-inherit-either")
    command = (
        sys.executable, "-c",
        "import json, os, pathlib; pathlib.Path(" + repr(str(report)) + ").write_text(json.dumps(dict(os.environ)))",
    )

    async def run() -> None:
        # Exiting before initialize is intentional: environment reporting runs
        # inside the actual subprocess launched by the ACP provider.
        from langgraph_acp import ACPConnectionError
        with pytest.raises(ACPConnectionError):
            await StdioACPProvider(name="report", command=command, env=explicit).connect()

    asyncio.run(run())
    environment = json.loads(report.read_text())
    assert set(environment) <= HOST_ENV_ALLOWLIST | set(explicit or {})
    for key, value in (explicit or {}).items():
        assert environment[key] == value


def test_explicit_configuration_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/host/path")
    assert agent_environment({"PATH": "/selected/path"})["PATH"] == "/selected/path"
