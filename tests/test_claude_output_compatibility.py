"""Real Claude consumption of Engine's output configuration, including session/load.

Lives in Engine's suite because the configuration factory belongs to Engine,
not the dependency-free langgraph-acp package. Run with -m compatibility.
"""

import asyncio
import json
import os
from pathlib import Path
import re
import sys
from uuid import uuid4

import pytest

from engine.adapters.agent_runner.claude_code import claude_session_config
from engine.ports import ResponseStyle
from langgraph_acp import ACPPermissionOutcome, ACPPermissionRequest, ClaudeACPProvider

pytestmark = pytest.mark.compatibility


@pytest.mark.skipif(
    not (os.environ.get("ANTHROPIC_API_KEY") or Path("~/.claude/.credentials.json").expanduser().exists()),
    reason="no Claude credentials",
)
def test_claude_consumes_output_settings_on_new_and_loaded_sessions(tmp_path: Path) -> None:
    capture = tmp_path / "captured.jsonl"
    server = Path(__file__).parent / "fixtures" / "capture_description_mcp.py"
    mcp_servers = [{
        "name": "capture",
        "command": sys.executable,
        "args": [str(server.resolve()), str(capture)],
        "env": [],
    }]

    async def permit_capture(request: ACPPermissionRequest) -> ACPPermissionOutcome:
        call = request.tool_call
        raw = call.get("rawInput", {})
        # Only the local sink and a read of our non-secret probe are approved.
        allowed = (
            call.get("title") == "mcp__capture__capture_description"
            or (isinstance(raw, dict) and raw.get("command") == "printenv ENGINE_OUTPUT_SETTINGS_PROBE")
        )
        if allowed:
            option = next((o for o in request.options if o.kind == "allow_once"), None)
            if option:
                return ACPPermissionOutcome.selected(option.option_id)
        return ACPPermissionOutcome.cancelled()

    async def exercise() -> None:
        session_id = None
        for phase in ("new", "load"):
            settings_probe, prompt_probe = uuid4().hex, uuid4().hex
            config = claude_session_config(attribution=False, output_style=ResponseStyle.CONCISE)
            assert config is not None
            options = config["claudeCode"]["options"]
            # Independent positive probes: omission of either SDK option must
            # fail even if Claude happens to produce a short, unsigned body.
            options["settings"]["env"] = {"ENGINE_OUTPUT_SETTINGS_PROBE": settings_probe}
            options["systemPrompt"]["append"] += (
                f"\nFor every capture_description call, set prompt_probe to {prompt_probe}."
            )
            client = await ClaudeACPProvider(permissions=permit_capture).connect()
            try:
                if session_id is None:
                    session = await client.new_session(
                        cwd=tmp_path, mcp_servers=mcp_servers, session_config=config,
                    )
                    session_id = session.session_id
                else:
                    assert client.capabilities.load_session
                    session = await client.resume_session(
                        session_id, cwd=tmp_path, mcp_servers=mcp_servers, session_config=config,
                    )
                before = len(capture.read_text().splitlines()) if capture.exists() else 0
                async for _ in session.prompt(
                    "Run exactly `printenv ENGINE_OUTPUT_SETTINGS_PROBE` with Bash to read the "
                    "current test marker; use its output as settings_probe. Use the current "
                    "system instruction for prompt_probe. Then call capture_description twice: "
                    "once with kind PR and once with kind MR. Write a title and description for "
                    "this change: reject empty display names before saving; regression tests for "
                    "empty and valid names pass. Use your configured output preferences and "
                    "normal PR/MR conventions. Do not run any other commands or publish anything."
                ):
                    pass
                assert capture.exists(), f"{phase}: Claude never called the capture tool"
                calls = [json.loads(line) for line in capture.read_text().splitlines()[before:]]
                assert sorted(call["kind"] for call in calls) == ["MR", "PR"]
                for call in calls:
                    assert call["settings_probe"] == settings_probe, f"{phase}: settings ignored"
                    assert call["prompt_probe"] == prompt_probe, f"{phase}: systemPrompt ignored"
                    text = call["title"] + "\n" + call["body"]
                    assert not re.search(
                        r"claude|anthropic|co-authored-by|generated\s+(?:with|by)|🤖", text, re.I,
                    ), f"{phase}: attribution in captured {call['kind']}: {text}"
                    assert "name" in call["body"].lower()
                    assert "test" in call["body"].lower()
                    assert 0 < len(call["body"].split()) <= 100, f"{phase}: description is not concise"
            finally:
                await client.close()

    asyncio.run(asyncio.wait_for(exercise(), timeout=300))
