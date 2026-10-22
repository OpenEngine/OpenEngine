"""Single-tenant local policy; verified delegation can supply another policy."""
from virtual_control_plane.domain.lifecycle import Rejected


class LocalAccount:
    async def authorize(self, actor, subject, owner):
        if actor != 'local' or owner not in (None, 'local'):
            raise Rejected('PERMISSION_DENIED', 'A verified local account is required')
