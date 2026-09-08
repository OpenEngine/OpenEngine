"""The real ACP adapters, launched and spoken to.

Every other test in this package runs against `fake_agent.py`, which proves our
half of the protocol: the frames we send, the updates we parse, the permission
outcome we send back. What it cannot prove is that the *adapters* still speak
the half we assume, and that is the half that moves without us.

`@agentclientprotocol/codex-acp` is not a rename of `@zed-industries/codex-acp`:
0.16 shipped per-platform native binaries, 1.9 is a JS package wrapping
`@openai/codex`. A jump like that is where the handshake, the session update
shapes, or the capability names drift, and asserting that `CODEX_ACP_COMMAND`
equals the string it was just set to would notice none of it.

Marked `compatibility` and deselected by default, for the reason the root
`pyproject.toml` gives: these need npm, a network, and -- for the turns --
somebody else's uptime, so a red run here should not read as "we broke
something". `cli-compatibility.yml` runs them on a schedule.

The split is deliberate:

* **The handshake needs no credentials.** An unauthenticated adapter still
  initializes and still advertises what it can do, so protocol drift is caught
  without depending on anyone being logged in.
* **The turns need them**, and skip without them, because an unauthenticated
  runner is a configuration fact rather than a compatibility result.

The permission round trip is covered through Claude rather than Codex, because
Codex will not perform it on request: under the session mode it starts in, it
runs both file writes and network commands without asking, and reaching the
mode that does ask means calling `session/set_mode`, which this package does
not expose. Claude's adapter asks for a plain file write. The parsing and the
outcome frame under test are this package's, not either agent's, so one live
exercise of them is the coverage -- which agent triggers it is incidental.
"""

import asyncio
import os
from pathlib import Path

import pytest

from langgraph_acp import (
    PROTOCOL_VERSION,
    ACPNode,
    ACPPermissionOutcome,
    ACPPermissionRequest,
    ClaudeACPProvider,
    CodexACPProvider,
)

pytestmark = pytest.mark.compatibility

#: What the adapter is expected to call itself. Checked rather than assumed:
#: `npx --yes` installs whatever the registry hands it, and a package that
#: resolves to something else is the drift this file exists to catch.
CODEX_ADAPTER = "@agentclientprotocol/codex-acp"


def _codex_authenticated() -> bool:
    """Whether Codex has something to authenticate with.

    `codex login` leaves `auth.json` under `CODEX_HOME`; CI supplies an API key
    instead. Either is enough to run a turn.
    """
    home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    return bool(os.environ.get("OPENAI_API_KEY")) or os.path.exists(
        os.path.join(home, "auth.json")
    )


def _claude_authenticated() -> bool:
    """The same question for Claude Code: a login on disk, or an API key."""
    return bool(os.environ.get("ANTHROPIC_API_KEY")) or os.path.exists(
        os.path.expanduser("~/.claude/.credentials.json")
    )


needs_codex = pytest.mark.skipif(
    not _codex_authenticated(),
    reason="no Codex credentials; the handshake test still runs",
)

needs_claude = pytest.mark.skipif(
    not _claude_authenticated(), reason="no Claude credentials"
)


def test_the_codex_adapter_completes_the_acp_handshake() -> None:
    """`connect()` initializes, and what comes back parses into named fields.

    Three assertions, three different failures: a protocol version this client
    does not speak, a package that is not the one we asked for, and an
    initialize result whose capabilities reached `raw` and nothing else
    because their shape moved.
    """

    async def handshake() -> None:
        client = await CodexACPProvider().connect()
        try:
            capabilities = client.capabilities

            assert capabilities.protocol_version == PROTOCOL_VERSION

            agent_info = capabilities.raw.get("agentInfo")
            assert isinstance(agent_info, dict)
            assert agent_info.get("name") == CODEX_ADAPTER

            # Codex has continued sessions, taken images, and offered a way to
            # log in since long before the move. Reading `False` here means the
            # capability shape changed, not that the agent lost the feature.
            assert capabilities.load_session
            assert capabilities.prompt_image
            assert capabilities.auth_methods
        finally:
            await client.close()

    asyncio.run(handshake())


@needs_codex
def test_a_turn_through_the_codex_adapter_answers() -> None:
    """The whole path: session, prompt, streamed updates, assembled result.

    `ACPNode` reads text out of `session/update` notifications and a stop
    reason out of the prompt response. Both are shapes the adapter owns, and
    neither is exercised by asserting on a command tuple.
    """
    result = asyncio.run(
        ACPNode(agent="codex")(
            "Reply with exactly the word: pineapple. No tools, no preamble."
        )
    )

    assert result.agent == "codex"
    assert result.session_id
    assert result.stop_reason == "end_turn"
    assert "pineapple" in result.message.lower()


@needs_claude
def test_a_permission_request_is_parsed_and_the_answer_honoured(
    tmp_path: Path,
) -> None:
    """The one round trip where the adapter calls *us*.

    It is also the one this package would fail quietly on: a request that did
    not parse means no option is ever chosen, which reads as a hung turn
    rather than as a protocol change.

    The written file is the assertion that matters. An outcome in the wrong
    shape would leave the agent waiting rather than writing, so the file
    existing is the only proof that what we sent back was understood.
    """
    requests: list[ACPPermissionRequest] = []

    async def answer(request: ACPPermissionRequest) -> ACPPermissionOutcome:
        requests.append(request)
        allow = next((o for o in request.options if o.kind == "allow_once"), None)
        if allow is None:
            return ACPPermissionOutcome.cancelled()
        return ACPPermissionOutcome.selected(allow.option_id)

    async def turn() -> None:
        client = await ClaudeACPProvider(permissions=answer).connect()
        try:
            session = await client.new_session(cwd=tmp_path)
            async for _ in session.prompt(
                "Create a file named proof.txt in the current directory "
                "containing the word pineapple. Then say done."
            ):
                pass
        finally:
            await client.close()

    asyncio.run(turn())

    assert requests, "the adapter never asked, so nothing here was exercised"
    asked = requests[0]
    assert asked.session_id
    assert [o.option_id for o in asked.options], "an option with no id cannot be chosen"
    assert any(o.kind == "allow_once" for o in asked.options)
    assert asked.tool_call.get("kind") == "edit"
    assert (tmp_path / "proof.txt").exists()
