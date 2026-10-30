from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tool_service import authorization as az

TOOL_SPEC = {
    "inputSchema": {
        "type": "object",
        "required": ["issueNumber", "body"],
        "properties": {
            "issueNumber": {"type": "integer", "minimum": 1},
            "body": {"type": "string", "minLength": 1},
        },
    }
}

NOW_MS = 1_761_144_000_000  # 2025-10-22T12:00:00Z, well inside the grants below
FUTURE = datetime(2026, 10, 22, tzinfo=timezone.utc)
PAST = datetime(2020, 1, 1, tzinfo=timezone.utc)


def grant(uid="g1", name="reviewer-comment-access", step="stepa_88_review_1", when=None, expires=FUTURE) -> az.Grant:
    return az.Grant(
        uid=uid,
        name=name,
        workspace="review-job-88",
        step_attempt_id=step,
        when=when,
        expires_at=expires,
    )


def decide(grants, ceilings=(), principal_kind="StepAttempt", principal_id="stepa_88_review_1", arguments=None, tool_uid="tool-uid-123"):
    return az.decide(
        tool_uid=tool_uid,
        grants=list(grants),
        ceilings=list(ceilings),
        principal_kind=principal_kind,
        principal_id=principal_id,
        workspace="review-job-88",
        arguments={"issueNumber": 42, "body": "hi", **(arguments or {})},
        now_ms=NOW_MS,
    )


# --- validate_condition: the grant's `when` at apply time ---


def test_a_valid_condition_passes() -> None:
    az.validate_condition("context.arguments.issueNumber == 42", TOOL_SPEC)


def test_a_condition_may_reference_context_now() -> None:
    az.validate_condition("context.now < 2000000000000", TOOL_SPEC)


def test_a_condition_may_reference_resource() -> None:
    az.validate_condition('resource == Tool::"tool-uid-123"', TOOL_SPEC)


def test_a_condition_cannot_reference_principal() -> None:
    with pytest.raises(az.CedarError, match="only `context` and `resource`"):
        az.validate_condition('principal == Workspace::"w"', TOOL_SPEC)


def test_a_condition_cannot_reference_action() -> None:
    with pytest.raises(az.CedarError, match="only `context` and `resource`"):
        az.validate_condition('action == Action::"invoke"', TOOL_SPEC)


def test_a_condition_with_a_type_error_is_rejected() -> None:
    with pytest.raises(az.CedarError, match="does not type-check"):
        az.validate_condition('context.arguments.issueNumber == "42"', TOOL_SPEC)


def test_a_condition_that_names_a_missing_argument_is_rejected() -> None:
    with pytest.raises(az.CedarError, match="does not type-check"):
        az.validate_condition("context.arguments.missing == 1", TOOL_SPEC)


def test_a_condition_that_does_not_parse_is_rejected() -> None:
    with pytest.raises(az.CedarError, match="does not parse"):
        az.validate_condition("context.arguments.issueNumber ==", TOOL_SPEC)


def test_an_omitted_condition_is_allowed() -> None:
    az.validate_condition(None, TOOL_SPEC)


# --- validate_ceiling: the operator's `forbid` at apply time ---


def test_a_forbid_ceiling_passes() -> None:
    az.validate_ceiling('forbid(principal, action == Action::"invoke", resource);')


def test_a_ceiling_may_name_any_tools_argument_pattern() -> None:
    # A ceiling is global, so it is not type-checked against one tool's schema.
    az.validate_ceiling('forbid(principal, action, resource) when { context.arguments.issueNumber == 43 };')


def test_a_ceiling_must_be_a_forbid() -> None:
    with pytest.raises(az.CedarError, match="must be a `forbid`"):
        az.validate_ceiling("permit(principal, action, resource);")


def test_a_ceiling_that_does_not_parse_is_rejected() -> None:
    with pytest.raises(az.CedarError, match="does not parse"):
        az.validate_ceiling("forbid(principal, action, resource))")


def test_an_omitted_ceiling_is_allowed() -> None:
    az.validate_ceiling(None)


# --- decide: a call is allowed only while a grant permits and no ceiling forbids ---


def test_a_matching_grant_allows_and_names_itself() -> None:
    result = decide([grant(when="context.arguments.issueNumber == 42")])
    assert result.allowed
    assert result.grant_refs == (az.GrantRef(name="reviewer-comment-access", uid="g1"),)


def test_a_grant_whose_condition_fails_denies() -> None:
    result = decide([grant(when="context.arguments.issueNumber == 43")])
    assert not result.allowed
    assert result.grant_refs == ()


def test_an_expired_grant_denies() -> None:
    result = decide([grant(expires=PAST)])
    assert not result.allowed
    assert result.grant_refs == ()


def test_no_grant_denies() -> None:
    result = decide([])
    assert not result.allowed
    assert result.grant_refs == ()


def test_a_step_grant_denies_a_call_from_another_step() -> None:
    result = decide([grant(step="stepa_88_review_1")], principal_id="stepa_88_other_1")
    assert not result.allowed


def test_a_workspace_wide_grant_allows_the_whole_workspace() -> None:
    result = decide([grant(step=None)], principal_kind="Workspace", principal_id="review-job-88")
    assert result.allowed
    assert result.grant_refs == (az.GrantRef(name="reviewer-comment-access", uid="g1"),)


def test_a_forbid_ceiling_overrides_a_matching_grant() -> None:
    ceiling = az.Ceiling(uid="c1", policy='forbid(principal, action, resource) when { context.arguments.issueNumber == 42 };')
    result = decide([grant(when="context.arguments.issueNumber == 42")], ceilings=[ceiling])
    assert not result.allowed
    assert result.grant_refs == ()


def test_a_ceiling_that_does_not_match_leaves_the_grant_in_force() -> None:
    ceiling = az.Ceiling(uid="c1", policy='forbid(principal, action, resource) when { context.arguments.issueNumber == 43 };')
    result = decide([grant(when="context.arguments.issueNumber == 42")], ceilings=[ceiling])
    assert result.allowed
    assert result.grant_refs == (az.GrantRef(name="reviewer-comment-access", uid="g1"),)


def test_a_blanket_forbid_denies_every_call_to_the_tool() -> None:
    ceiling = az.Ceiling(uid="c1", policy='forbid(principal, action == Action::"invoke", resource == Tool::"tool-uid-123");')
    result = decide([grant()], ceilings=[ceiling])
    assert not result.allowed


def test_a_ceiling_named_by_a_denial_is_not_a_grant_ref() -> None:
    ceiling = az.Ceiling(uid="c1", policy='forbid(principal, action, resource) when { context.arguments.issueNumber == 42 };')
    result = decide([grant(when="context.arguments.issueNumber == 42")], ceilings=[ceiling])
    assert all(ref.uid != "c1" for ref in result.grant_refs)
