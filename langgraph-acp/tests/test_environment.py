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


@pytest.mark.parametrize("override", [None, "/explicit/account"])
def test_claude_preserves_only_its_named_host_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, override: str | None,
) -> None:
    from langgraph_acp import ClaudeACPProvider

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/selected/account")
    monkeypatch.setenv("UNSELECTED_CLAUDE_SETTING", "must-not-inherit")
    report = tmp_path / "environment.json"
    fake = Path(__file__).with_name("fake_agent.py")
    wrapper = tmp_path / "report.py"
    wrapper.write_text(
        "import json, os, pathlib, runpy\n"
        f"pathlib.Path({str(report)!r}).write_text(json.dumps(dict(os.environ)))\n"
        f"runpy.run_path({str(fake)!r}, run_name='__main__')\n"
    )

    async def run() -> None:
        client = await ClaudeACPProvider(
            command=(sys.executable, str(wrapper)),
            env=None if override is None else {"CLAUDE_CONFIG_DIR": override},
        ).connect()
        await client.close()

    asyncio.run(run())
    environment = json.loads(report.read_text())
    assert environment["CLAUDE_CONFIG_DIR"] == (override or "/selected/account")
    assert set(environment) <= HOST_ENV_ALLOWLIST | {"CLAUDE_CONFIG_DIR"}
