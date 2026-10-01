"""Connected and disconnected runs: one vocabulary, read the same way everywhere."""

import pytest

from engine.domain import MODE_INPUT, ForgeMode, forge_mode
from engine.graph_runtime.inputs import mode_input, resolve_inputs
from engine.graph_runtime_langgraph.components.forge import (
    PUBLISH_CHANGE,
    PUBLISH_SUMMARY,
    ByMode,
)


@pytest.mark.parametrize(("inputs", "mode"), [
    (None, ForgeMode.CONNECTED),
    ({}, ForgeMode.CONNECTED),
    ({MODE_INPUT: "connected"}, ForgeMode.CONNECTED),
    ({MODE_INPUT: "disconnected"}, ForgeMode.DISCONNECTED),
    ({MODE_INPUT: "something else"}, ForgeMode.CONNECTED),
])
def test_a_run_is_connected_unless_its_inputs_say_otherwise(inputs, mode):
    assert forge_mode(inputs) is mode


def test_the_mode_input_offers_both_modes_and_rejects_others():
    declared = mode_input()
    assert declared.name == MODE_INPUT
    assert declared.choices == ("connected", "disconnected")
    assert resolve_inputs((declared,), {}) == {MODE_INPUT: "connected"}
    assert mode_input(ForgeMode.DISCONNECTED).default == "disconnected"
    with pytest.raises(ValueError):
        resolve_inputs((declared,), {MODE_INPUT: "offline"})


def test_snippets_are_worded_for_the_run_s_mode_and_formatted():
    snippet = ByMode(connected="post to {pr_url}", disconnected="keep it")
    assert snippet({}, pr_url="https://pr") == "post to https://pr"
    assert snippet({"inputs": {MODE_INPUT: "disconnected"}}, pr_url="x") == "keep it"
    offline = {"inputs": {MODE_INPUT: "disconnected"}}
    for text in (PUBLISH_CHANGE(offline), PUBLISH_SUMMARY(offline, pr_url="")):
        assert "open_pull_request" not in text and "add_comment" not in text
    assert "open_pull_request" in PUBLISH_CHANGE({})
