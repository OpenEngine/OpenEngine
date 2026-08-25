"""Names resolve to providers, and a provider is enough to reach an agent.

The acceptance criterion for this layer is one line -- `registry.resolve("codex")`
returns something that can connect -- but the reason it has to keep working is
longer: it is what lets a graph say `ACPNode(agent="codex")` without importing
anything that knows what Codex is.
"""

import pytest

from langgraph_acp import (
    ACPAgentNotFoundError,
    ACPAgentProvider,
    ACPAgentRegistry,
    CodexACPProvider,
    StdioACPProvider,
    default_registry,
)


def test_codex_resolves_to_a_provider() -> None:
    """The ticket's acceptance criterion, verbatim."""
    provider = default_registry().resolve("codex")

    assert isinstance(provider, ACPAgentProvider)
    assert provider.name == "codex"


def test_the_default_registry_is_shared() -> None:
    """An application registers its agents once, and every node sees them."""
    assert default_registry() is default_registry()
    assert "codex" in default_registry().names


def test_an_unregistered_name_says_what_is_registered() -> None:
    """Nearly always a typo, so the message answers the next question."""
    with pytest.raises(ACPAgentNotFoundError, match="codex") as caught:
        default_registry().resolve("codecs")

    assert caught.value.agent == "codecs"


def test_an_empty_registry_still_explains_itself() -> None:
    with pytest.raises(ACPAgentNotFoundError, match="registered: none"):
        ACPAgentRegistry().resolve("codex")


def test_a_provider_registers_under_its_own_name() -> None:
    registry = ACPAgentRegistry([StdioACPProvider(name="gemini", command=["gemini"])])

    assert registry.names == ("gemini",)
    assert registry.resolve("gemini").name == "gemini"
    assert "gemini" in registry


def test_shadowing_a_registered_agent_has_to_be_meant() -> None:
    """A graph resolving to something its author did not read is a bad surprise."""
    registry = ACPAgentRegistry([CodexACPProvider()])

    with pytest.raises(ValueError, match="already registered"):
        registry.register(StdioACPProvider(name="codex", command=["elsewhere"]))

    registry.register(StdioACPProvider(name="codex", command=["elsewhere"]), replace=True)
    assert registry.resolve("codex").name == "codex"


def test_the_same_agent_can_be_registered_twice_under_different_settings() -> None:
    registry = ACPAgentRegistry(
        [CodexACPProvider(), CodexACPProvider(name="codex-in-container")]
    )

    assert registry.names == ("codex", "codex-in-container")


def test_codex_is_reached_through_its_acp_adapter() -> None:
    """Codex itself does not speak ACP; this is the wrapper that does."""
    assert CodexACPProvider().command == ("npx", "--yes", "@zed-industries/codex-acp")
    assert isinstance(CodexACPProvider(), ACPAgentProvider)


def test_a_provider_can_be_pointed_at_a_local_adapter() -> None:
    assert CodexACPProvider(command=["codex-acp"]).command == ("codex-acp",)


def test_a_provider_without_a_command_fails_at_construction() -> None:
    with pytest.raises(ValueError, match="command"):
        StdioACPProvider(name="nothing", command=[])


def test_a_lone_string_is_refused_where_a_command_belongs() -> None:
    """`command="codex acp"` would launch a program named after the whole line."""
    with pytest.raises(TypeError, match="command"):
        StdioACPProvider(name="codex", command="codex-acp")


def test_providers_compare_by_value() -> None:
    assert CodexACPProvider() == CodexACPProvider()
    assert CodexACPProvider() != CodexACPProvider(name="other")
