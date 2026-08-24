"""What the configured policy says about one request, before anybody is asked.

`engine.runtime.config` reads Engine's approval vocabulary and deliberately
stops there. A runner's `PermissionTranslator` says which capability one
provider request is asking for. This module is the join: policy in, one
classified request in, one of three answers out.

Three answers because "not allowed" and "ask somebody" are different
statements, and collapsing them would be a permission decision made by us:

* **allow** -- the configuration already agreed to this. Nobody is shown it.
* **ask** -- the configuration has not ruled on it. A person decides.
* **deny** -- the configuration ruled against it. Nobody is shown it either,
  because a request that was always going to be refused is not a question.

A request no runner could classify is `None`, and asks: fail closed is the only
safe direction, and the runner that could not name the capability is the one
that knows least about what it would do.

Shell is the capability with rules of its own, and its patterns are consulted
before the capability list rather than after. `allow = ["read"]` beside a bash
`allow` pattern is the ordinary shape of a real policy -- shell is not granted
wholesale, and these few commands are -- so the patterns have to be able to
allow what the capability list does not.
"""

from enum import Enum
from fnmatch import fnmatchcase

from engine.ports.permissions import ApprovalCapability, PermissionScope
from engine.runtime.config import ApprovalConfig, BashApprovalConfig


class PolicyDecision(Enum):
    """What the configuration says about a request, on its own."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


def policy_decision_for(
    policy: ApprovalConfig,
    scope: PermissionScope | None,
    *,
    read_only: bool = False,
) -> PolicyDecision:
    """The configured answer for a request classified as `scope`.

    A bash pattern beats `auto_approve` in both directions. `auto_approve` is a
    blanket "stop asking me"; a pattern is a sentence somebody wrote about one
    command, and the specific statement is the one they meant.

    `read_only` is the turn saying its agent is one that never changes anything,
    and it is answered before the configuration rather than by it. A deployment
    that allows edits is describing what an agent *asked to change something*
    may do, not granting one that was never asked to; so nothing survives this
    -- not `auto_approve`, not a shell allow pattern, and not a request no
    runner could classify, since a request nobody can name cannot be shown to be
    a read.
    """
    if read_only and (
        scope is None or scope.capability is not ApprovalCapability.READ
    ):
        return PolicyDecision.DENY
    if scope is None:
        return PolicyDecision.ALLOW if policy.auto_approve else PolicyDecision.ASK
    if scope.capability is ApprovalCapability.BASH:
        matched = _bash_decision(policy.bash, scope.value)
        if matched is not None:
            return matched
    if policy.auto_approve or scope.capability in policy.allow:
        return PolicyDecision.ALLOW
    return PolicyDecision.ASK


def _bash_decision(
    bash: BashApprovalConfig, command: str | None
) -> PolicyDecision | None:
    """The rule that names this command, or `None` when none does.

    Deny, then ask, then allow: where two rules both match, the more restrictive
    one is the one the command was written into. A request that arrived without
    a command cannot be matched against anything, so no rule names it and the
    capability list decides.
    """
    if not command:
        return None
    collapsed = " ".join(command.split())
    for patterns, decision in (
        (bash.deny, PolicyDecision.DENY),
        (bash.ask, PolicyDecision.ASK),
        (bash.allow, PolicyDecision.ALLOW),
    ):
        if any(_matches(collapsed, pattern) for pattern in patterns):
            return decision
    return None


def _matches(command: str, pattern: str) -> bool:
    """Glob the pattern against one collapsed command line.

    A trailing `**` also matches nothing at all, so `git status **` covers a
    bare `git status`: an argument list the pattern says it does not care about
    is allowed to be empty. Without that, every pattern would need writing
    twice.
    """
    if fnmatchcase(command, pattern):
        return True
    head, separator, tail = pattern.rpartition(" ")
    return bool(separator) and tail == "**" and fnmatchcase(command, head)


__all__ = ["PolicyDecision", "policy_decision_for"]
