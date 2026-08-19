"""Configured approval policy is narrow, ordered, and fail-closed."""

import asyncio

from engine.domain import ApprovalDecision, ApprovalKind
from engine.ports import ApprovalCapability, ApprovalRequest, PermissionScope
from engine.runtime import ApprovalConfig, ApprovalPolicy, BashApprovalConfig


class CommandTranslator:
    def scope_for(self, request: ApprovalRequest) -> PermissionScope | None:
        if request.command is None:
            return None
        return PermissionScope(ApprovalCapability.BASH, request.command)


def _request(command: str | None) -> ApprovalRequest:
    return ApprovalRequest("request-1", ApprovalKind.COMMAND_EXECUTION, command=command)


def test_explicit_bash_rules_use_deny_ask_allow_precedence() -> None:
    policy = ApprovalPolicy(
        ApprovalConfig(
            allow=(ApprovalCapability.BASH,),
            bash=BashApprovalConfig(
                allow=("git **",),
                ask=("git push **",),
                deny=("git push --force **",),
            ),
        )
    )

    assert policy.decision_for(
        PermissionScope(ApprovalCapability.BASH, "git status")
    ) is ApprovalDecision.ACCEPT
    assert policy.decision_for(
        PermissionScope(ApprovalCapability.BASH, "git push origin topic")
    ) is None
    assert policy.decision_for(
        PermissionScope(ApprovalCapability.BASH, "git push --force origin topic")
    ) is ApprovalDecision.CANCEL


def test_trailing_double_star_matches_zero_or_more_arguments() -> None:
    policy = ApprovalPolicy(
        ApprovalConfig(bash=BashApprovalConfig(allow=("gh auth status **",)))
    )

    assert policy.decision_for(
        PermissionScope(ApprovalCapability.BASH, "gh auth status")
    ) is ApprovalDecision.ACCEPT
    assert policy.decision_for(
        PermissionScope(ApprovalCapability.BASH, "gh auth status --hostname github.com")
    ) is ApprovalDecision.ACCEPT


def test_capability_allow_and_auto_approve_accept_classified_requests() -> None:
    scoped = ApprovalPolicy(
        ApprovalConfig(allow=(ApprovalCapability.EDIT,))
    )
    automatic = ApprovalPolicy(ApprovalConfig(auto_approve=True, allow=()))

    edit = PermissionScope(ApprovalCapability.EDIT)
    web = PermissionScope(ApprovalCapability.WEB)
    assert scoped.decision_for(edit) is ApprovalDecision.ACCEPT
    assert scoped.decision_for(web) is None
    assert automatic.decision_for(web) is ApprovalDecision.ACCEPT
    assert automatic.decision_for(None) is None


def test_handler_asks_through_fallback_or_cancels_without_one() -> None:
    policy = ApprovalPolicy(ApprovalConfig())
    request = _request("gh pr list")
    presented: list[ApprovalRequest] = []

    async def ask(pending: ApprovalRequest) -> ApprovalDecision:
        presented.append(pending)
        return ApprovalDecision.ACCEPT

    async def scenario() -> tuple[ApprovalDecision, ApprovalDecision]:
        asked = await policy.handler(CommandTranslator(), ask)(request)
        headless = await policy.handler(CommandTranslator())(request)
        return asked, headless

    asked, headless = asyncio.run(scenario())

    assert asked is ApprovalDecision.ACCEPT
    assert headless is ApprovalDecision.CANCEL
    assert presented == [request]
