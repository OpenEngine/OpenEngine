"""Browser provider scripts remain explicit when ACP filters host variables."""

import asyncio
import json

from langgraph_acp import ACPAgentRegistry, ACPNode, StdioACPProvider, agent_environment
from provider_fakes import SCRIPT_ENVIRONMENT_VARIABLE, fake_acp


def test_scripted_launcher_does_not_require_host_environment_inheritance(tmp_path, monkeypatch):
    script = tmp_path / "scenario's script.json"
    monkeypatch.setenv(SCRIPT_ENVIRONMENT_VARIABLE, str(tmp_path / "unselected.json"))
    executable = fake_acp(tmp_path, script=script)
    node = ACPNode(
        agent="scripted", working_directory=tmp_path,
        registry=ACPAgentRegistry([StdioACPProvider(name="scripted", command=(executable,))]),
    )

    async def run():
        for reply in ("First scripted response.", "Updated scripted response."):
            script.write_text(json.dumps({"scenarios": [{"steps": [{"type": "say", "text": reply}]}]}))
            result = await node("Read the repository.")
            assert result.message == reply

    asyncio.run(run())
    assert SCRIPT_ENVIRONMENT_VARIABLE not in agent_environment()
