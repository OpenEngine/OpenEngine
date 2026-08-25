"""Requested settings and demanded capabilities."""

import pytest

from langgraph_acp import ACPConfig, ACPRequirements, UnsupportedOption


def test_an_unsupported_setting_is_an_error_by_default() -> None:
    """A setting that vanishes silently produces a confidently wrong answer."""
    assert ACPConfig().unsupported is UnsupportedOption.ERROR


def test_an_unsupported_policy_string_is_the_policy_it_names() -> None:
    assert ACPConfig(unsupported="ignore") == ACPConfig(
        unsupported=UnsupportedOption.IGNORE
    )


def test_an_unknown_unsupported_policy_fails_at_construction() -> None:
    with pytest.raises(ValueError, match="explode"):
        ACPConfig(unsupported="explode")


def test_settings_are_addressed_semantically_or_exactly() -> None:
    config = ACPConfig(
        by_category={"mode": "code", "thought_level": "high"},
        by_id={"codex.reasoning_effort": "high"},
    )

    assert config.by_category["mode"] == "code"
    assert config.by_id["codex.reasoning_effort"] == "high"


def test_configs_compare_by_value() -> None:
    assert ACPConfig(by_category={"mode": "code"}) == ACPConfig(
        by_category={"mode": "code"}
    )
    assert ACPConfig(by_category={"mode": "code"}) != ACPConfig(
        by_category={"mode": "chat"}
    )


def test_a_config_does_not_share_the_mapping_it_was_given() -> None:
    """One node's settings must not change because another node's dict did."""
    requested = {"mode": "code"}
    config = ACPConfig(by_category=requested)

    requested["mode"] = "chat"

    assert config.by_category == {"mode": "code"}


def test_requirements_demand_nothing_by_default() -> None:
    """`False` means "this workflow does not care", not "must not support"."""
    assert ACPRequirements().required == ()


def test_requirements_name_what_they_demand() -> None:
    assert ACPRequirements(resume=True, mcp=True, elicitation=False).required == (
        "resume",
        "mcp",
    )
