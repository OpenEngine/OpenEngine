"""Evaluate provider approval requests against Engine's configured policy."""

from __future__ import annotations

from fnmatch import fnmatchcase

from engine.domain import ApprovalDecision
from engine.ports import (
    ApprovalCapability,
    ApprovalHandler,
    ApprovalRequest,
    PermissionScope,
    PermissionTranslator,
)
from engine.runtime.config import ApprovalConfig


class ApprovalPolicy:
    """Turn provider-neutral permission scopes into approval decisions.

    Explicit Bash rules take precedence in the safest order: deny, ask, allow.
    An unmatched or unclassified request is left to the caller, which may ask a
    person or fail closed when no interactive presenter exists.
    """

    def __init__(self, config: ApprovalConfig) -> None:
        self._config = config

    def decision_for(self, scope: PermissionScope | None) -> ApprovalDecision | None:
        if scope is None:
            return None

        if scope.capability is ApprovalCapability.BASH:
            command = scope.value or ""
            bash = self._config.bash
            if _matches(command, bash.deny):
                return ApprovalDecision.CANCEL
            if _matches(command, bash.ask):
                return None
            if _matches(command, bash.allow):
                return ApprovalDecision.ACCEPT

        if scope.capability in self._config.allow or self._config.auto_approve:
            return ApprovalDecision.ACCEPT
        return None

    def handler(
        self,
        translator: PermissionTranslator,
        fallback: ApprovalHandler | None = None,
    ) -> ApprovalHandler:
        """Build a provider callback, optionally asking a person on no match."""

        async def decide(request: ApprovalRequest) -> ApprovalDecision:
            decision = self.decision_for(translator.scope_for(request))
            if decision is not None:
                return decision
            if fallback is not None:
                return await fallback(request)
            # Workflows have no live human presenter. Cancelling is the only
            # safe answer to an ask rule, an unmatched scope, or a request from
            # a provider version the translator does not yet understand.
            return ApprovalDecision.CANCEL

        return decide


def _matches(command: str, patterns: tuple[str, ...]) -> bool:
    return any(_matches_pattern(command, pattern) for pattern in patterns)


def _matches_pattern(command: str, pattern: str) -> bool:
    # ``command **`` means the command with zero or more trailing arguments.
    # This preserves the configuration's shell-oriented spelling while also
    # allowing exact invocations such as ``gh auth status``.
    if pattern.endswith(" **"):
        prefix = pattern[:-3]
        return command == prefix or fnmatchcase(command, pattern)
    return fnmatchcase(command, pattern)


__all__ = ["ApprovalPolicy"]
