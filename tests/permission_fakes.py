"""Fail-closed permission translation for runner test doubles."""

from engine.ports import ApprovalRequest, PermissionScope


class UnclassifiedPermissionTranslator:
    """A test runner knows no provider permissions unless a test says otherwise."""

    def scope_for(self, request: ApprovalRequest) -> PermissionScope | None:
        return None


UNCLASSIFIED_PERMISSION_TRANSLATOR = UnclassifiedPermissionTranslator()
