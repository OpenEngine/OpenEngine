"""The transport both concierges expose their one tool through.

Tested once, here, rather than once per surface. That is the whole point of it
being one implementation: a protocol answer that is wrong is wrong for every
surface at once, and a copy of these tests per surface is how two copies of the
code stayed in step until they didn't.

Each surface's own rules -- what it will accept and what it answers -- are not
here. They live with the broker that decides them.
"""

import asyncio

import pytest

from engine.github_concierge import github_egress
from engine.single_tool_mcp import PROTOCOL_VERSION, mcp_response, rpc_error, rpc_result
from engine.slack_concierge import slack_egress

#: Every surface served over this transport, with the one tool it grants. A
#: surface that stops appearing here has stopped sharing the transport, which
#: should be a decision rather than a silent omission.
SURFACES = [
    pytest.param(slack_egress, "create_workorder", id="slack"),
    pytest.param(github_egress, "continue_workorder", id="github"),
]


def _answer(request, surface):
    async def scenario():
        return await mcp_response(
            "127.0.0.1", 0, "tok", request,
            tool_spec=surface._TOOL_SPEC,
            server_info_name=surface._SERVER_INFO_NAME,
        )

    return asyncio.run(scenario())


@pytest.mark.parametrize("surface, tool_name", SURFACES)
def test_initialize_returns_the_servers_own_protocol_version(surface, tool_name):
    """A version this server does not implement is not made true by agreeing."""
    answer = _answer(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "1999-01-01",
            "clientInfo": {"name": "test", "version": "1"},
        }},
        surface,
    )
    assert answer["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert answer["result"]["serverInfo"]["name"] == surface._SERVER_INFO_NAME


@pytest.mark.parametrize("surface, tool_name", SURFACES)
def test_a_surface_offers_exactly_the_one_tool_it_grants(surface, tool_name):
    """One tool, and it is the surface's. Not a menu the agent chooses from."""
    answer = _answer({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, surface)
    assert [tool["name"] for tool in answer["result"]["tools"]] == [tool_name]


@pytest.mark.parametrize("surface, tool_name", SURFACES)
def test_notifications_are_answered_with_nothing(surface, tool_name):
    """JSON-RPC notifications take no reply, and a reply to one is a protocol error."""
    assert _answer(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}, surface
    ) is None


@pytest.mark.parametrize("surface, tool_name", SURFACES)
def test_an_unknown_method_is_refused(surface, tool_name):
    """Serving one tool means serving no resources, prompts, or completions."""
    answer = _answer(
        {"jsonrpc": "2.0", "id": 3, "method": "resources/list"}, surface
    )
    assert answer["error"]["code"] == -32601


@pytest.mark.parametrize("surface, tool_name", SURFACES)
@pytest.mark.parametrize("request_, code", [
    ("not an object", -32600),
    # A tool call's answer has to be addressed to something, and `None` is not
    # an id: JSON-RPC spells that a notification, which takes no reply at all.
    ({"jsonrpc": "2.0", "method": "tools/call", "params": {}}, -32600),
    ({"jsonrpc": "2.0", "id": True, "method": "tools/call", "params": {}}, -32600),
    ({"jsonrpc": "2.0", "id": 4, "method": "tools/call"}, -32602),
    ({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": []}, -32602),
])
def test_a_malformed_call_is_refused_before_it_reaches_the_host(
    surface, tool_name, request_, code, monkeypatch
):
    """Refused here, so a broker only ever sees calls worth deciding about.

    Nothing reaches the host: these never open the connection, which keeps a
    malformed request from being a way to knock on the door repeatedly.
    """
    async def unreachable(*_arguments, **_kwargs):
        raise AssertionError("a malformed call must not reach the host")

    monkeypatch.setattr("engine.single_tool_mcp.forward_call", unreachable)
    assert _answer(request_, surface)["error"]["code"] == code


def test_a_refusal_by_the_host_is_a_result_the_agent_can_read(monkeypatch):
    """Not a protocol error: the question was well formed and the answer is no.

    An agent that asked properly and was declined can act on that -- say so, or
    ask for something else. A JSON-RPC error says its request was malformed,
    which would send it to fix the wrong thing.
    """
    async def refuse(*_arguments, **_kwargs):
        return {"ok": False, "error": "no work order for this pull request"}

    monkeypatch.setattr("engine.single_tool_mcp.forward_call", refuse)
    answer = _answer(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "continue_workorder", "arguments": {"prompt": "go"}}},
        github_egress,
    )
    assert "error" not in answer
    assert answer["result"]["isError"] is True
    assert answer["result"]["content"] == [
        {"type": "text", "text": "no work order for this pull request"}
    ]


def test_what_the_host_answered_is_carried_through_unexamined(monkeypatch):
    """The transport reports the surface's answer; it does not have one of its own."""
    async def accept(*_arguments, **_kwargs):
        return {"ok": True, "text": "delivered", "data": {"run_id": "run-1"}}

    monkeypatch.setattr("engine.single_tool_mcp.forward_call", accept)
    answer = _answer(
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "continue_workorder", "arguments": {"prompt": "go"}}},
        github_egress,
    )
    assert answer["result"] == {
        "content": [{"type": "text", "text": "delivered"}],
        "structuredContent": {"run_id": "run-1"},
    }


def test_the_tool_name_is_forwarded_rather_than_judged_here(monkeypatch):
    """Whether a name is this run's tool is the broker's call, not the shim's.

    The shim runs in the subprocess the agent's CLI started, on the far side of
    the credential; deciding there would be deciding somewhere an agent can
    reach. It passes the name on, and the broker -- which holds the secret --
    refuses it.
    """
    seen = []

    async def remember(_host, _port, _token, _request_id, name, arguments):
        seen.append((name, arguments))
        return {"ok": False, "error": f"unknown concierge tool: {name}"}

    monkeypatch.setattr("engine.single_tool_mcp.forward_call", remember)
    _answer(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
         "params": {"name": "rm_rf", "arguments": {"prompt": "go"}}},
        github_egress,
    )
    assert seen == [("rm_rf", {"prompt": "go"})]


def test_the_credential_is_never_passed_on_the_command_line():
    """Anyone on this host can read argv; only this user can read the file."""
    async def scenario():
        async with github_egress.FeedbackBroker(continue_workorder=_unused) as broker:
            return broker.config, broker._token

    config, token = asyncio.run(scenario())
    assert token not in " ".join(config["args"])
    assert "--token-file" in config["args"]
    assert config["args"][:2] == ["-m", "engine.github_concierge.github_egress"]
    # An inherited environment is a way to reach the host too, so the
    # subprocess is given none of one.
    assert config["env"] == []


async def _unused(_prompt):
    raise AssertionError("the transport must not call the tool")


def test_a_broker_that_was_never_started_has_nothing_to_describe():
    """No socket, no descriptor: a CLI pointed at a closed port would only hang."""
    with pytest.raises(RuntimeError, match="has not been started"):
        github_egress.FeedbackBroker(continue_workorder=_unused).config


@pytest.mark.parametrize("surface, tool_name", SURFACES)
def test_two_brokers_do_not_share_a_credential(surface, tool_name):
    """One run's secret opens one run's tool, and nobody else's."""
    brokers = {
        slack_egress.ConciergeBroker(create_workorder=_unused)._token
        if surface is slack_egress
        else github_egress.FeedbackBroker(continue_workorder=_unused)._token
        for _ in range(2)
    }
    assert len(brokers) == 2


def test_rpc_envelopes_are_well_formed_json_rpc():
    assert rpc_result(1, {"a": 2}) == {"jsonrpc": "2.0", "id": 1, "result": {"a": 2}}
    assert rpc_error(None, -32700, "Parse error") == {
        "jsonrpc": "2.0", "id": None,
        "error": {"code": -32700, "message": "Parse error"},
    }
