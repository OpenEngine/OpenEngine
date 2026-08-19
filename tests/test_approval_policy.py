"""What the configured policy says, before any of it reaches a provider.

`engine.toml` is the only place a deployment writes its permissions down, so
these are the sentences it can write. The interesting cases are the ones where
two rules could both apply: a capability list beside a shell pattern, a blanket
`auto_approve` beside a `deny`. Each of those resolves one way, and quietly
resolving it the other way is how a policy comes to allow what nobody meant.
"""

import pytest

from engine.ports import ApprovalCapability, PermissionScope
from engine.runtime import (
    ApprovalConfig,
    BashApprovalConfig,
    PolicyDecision,
    policy_decision_for,
)

READ = PermissionScope(ApprovalCapability.READ)
EDIT = PermissionScope(ApprovalCapability.EDIT)


def _bash(command: str) -> PermissionScope:
    return PermissionScope(ApprovalCapability.BASH, command)


def test_a_capability_that_was_not_granted_is_asked_about_rather_than_refused() -> None:
    """Absence from `allow` is silence, and silence is a question for a person.

    The distinction the whole enforcement rests on: a capability nobody ruled on
    can still be allowed by whoever is watching, and only `deny` refuses.
    """
    policy = ApprovalConfig(allow=(ApprovalCapability.READ,))

    assert policy_decision_for(policy, READ) is PolicyDecision.ALLOW
    assert policy_decision_for(policy, EDIT) is PolicyDecision.ASK


def test_a_request_no_runner_could_classify_is_always_asked_about() -> None:
    """Fail closed: the runner that could not name it knows least about it."""
    granted = ApprovalConfig(allow=tuple(ApprovalCapability))

    assert policy_decision_for(granted, None) is PolicyDecision.ASK


def test_shell_patterns_allow_what_the_capability_list_does_not() -> None:
    """The ordinary shape of a real policy: no shell, except these commands."""
    policy = ApprovalConfig(
        allow=(ApprovalCapability.READ,),
        bash=BashApprovalConfig(allow=("uv run pytest **", "git add **")),
    )

    assert policy_decision_for(policy, _bash("uv run pytest tests/")) is (
        PolicyDecision.ALLOW
    )
    assert policy_decision_for(policy, _bash("rm -rf /")) is PolicyDecision.ASK


def test_a_trailing_wildcard_also_matches_no_arguments_at_all() -> None:
    """Otherwise every pattern would need writing twice."""
    policy = ApprovalConfig(bash=BashApprovalConfig(allow=("git status **",)))

    assert policy_decision_for(policy, _bash("git status")) is PolicyDecision.ALLOW
    assert policy_decision_for(policy, _bash("git status -sb")) is PolicyDecision.ALLOW


def test_a_command_is_matched_however_the_provider_spaced_it() -> None:
    """Providers re-render what they ask about; the rule is about the command."""
    policy = ApprovalConfig(bash=BashApprovalConfig(allow=("git add **",)))

    assert policy_decision_for(policy, _bash("git   add   -A")) is PolicyDecision.ALLOW


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("sudo rm -rf /", PolicyDecision.DENY),
        ("git push --force", PolicyDecision.ASK),
        ("git status", PolicyDecision.ALLOW),
    ],
)
def test_the_most_restrictive_matching_rule_wins(
    command: str, expected: PolicyDecision
) -> None:
    """Where two rules both name a command, it was written into the narrow one."""
    policy = ApprovalConfig(
        allow=(ApprovalCapability.BASH,),
        bash=BashApprovalConfig(
            allow=("git **",), ask=("git push **",), deny=("sudo **",)
        ),
    )

    assert policy_decision_for(policy, _bash(command)) is expected


def test_auto_approve_allows_everything_a_pattern_has_not_ruled_on() -> None:
    """Including the unclassifiable: "stop asking me" is what it says.

    A pattern still beats it in both directions -- the blanket is a preference,
    and a rule written about one command is a decision about that command.
    """
    policy = ApprovalConfig(
        auto_approve=True,
        allow=(),
        bash=BashApprovalConfig(ask=("git push **",), deny=("sudo **",)),
    )

    assert policy_decision_for(policy, EDIT) is PolicyDecision.ALLOW
    assert policy_decision_for(policy, None) is PolicyDecision.ALLOW
    assert policy_decision_for(policy, _bash("ls")) is PolicyDecision.ALLOW
    assert policy_decision_for(policy, _bash("git push")) is PolicyDecision.ASK
    assert policy_decision_for(policy, _bash("sudo id")) is PolicyDecision.DENY


def test_a_shell_request_without_a_command_falls_back_to_the_capability() -> None:
    """No pattern can name a command we were not told, so none does."""
    policy = ApprovalConfig(
        allow=(ApprovalCapability.BASH,), bash=BashApprovalConfig(deny=("**",))
    )
    unnamed = PermissionScope(ApprovalCapability.BASH, None)

    assert policy_decision_for(policy, unnamed) is PolicyDecision.ALLOW
