"""Permission translation for runner test doubles.

Two, because a runner's reading of its own provider is what the configured
policy is applied to: a test double with no reading gets asked about
everything, which is the safe default and the wrong one to write a policy test
against.
"""

from engine.domain import ApprovalKind
from engine.ports import ApprovalCapability, ApprovalRequest, PermissionScope


class UnclassifiedPermissionTranslator:
    """A test runner knows no provider permissions unless a test says otherwise."""

    def scope_for(self, request: ApprovalRequest) -> PermissionScope | None:
        return None


class KindPermissionTranslator:
    """Reads requests by kind, as both real adapters do for their own."""

    def scope_for(self, request: ApprovalRequest) -> PermissionScope | None:
        if request.kind is ApprovalKind.COMMAND_EXECUTION:
            return PermissionScope(ApprovalCapability.BASH, request.command)
        if request.kind is ApprovalKind.FILE_CHANGE:
            return PermissionScope(ApprovalCapability.EDIT)
        return None


UNCLASSIFIED_PERMISSION_TRANSLATOR = UnclassifiedPermissionTranslator()
KIND_PERMISSION_TRANSLATOR = KindPermissionTranslator()
