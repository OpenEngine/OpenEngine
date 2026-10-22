"""Recover first setup without ever recreating an established, lost journal."""
import json

from virtual_control_plane.domain.runtime import invalid
from virtual_control_plane.provisioning.broker import private_json


class Initialization:
    def __init__(self, directory, target, initialize):
        self.marker = directory / 'binding.json'
        self.pending = directory / 'initializing.json'
        self.target = target
        self.established = self.marker.exists()
        if self.established:
            if json.loads(self.marker.read_text()) != target:
                invalid('Journal binding changed; fence runtime before replacement')
            if not list(directory.glob('*.sqlite*')):
                invalid('Lost runtime journal; refusing empty recovery')
        elif self.pending.exists():
            if json.loads(self.pending.read_text()) != target:
                invalid('Journal initialization binding changed')
        else:
            if not initialize or list(directory.glob('*.sqlite*')):
                invalid('Journal missing or unbound; fence runtime before replacement')
            # Durable intent precedes migration/validation. No command listener
            # may start until recover() commits and finish() seals the binding.
            private_json(self.pending, target)

    async def check(self, store):
        async with store.transaction() as tx:
            meta = await tx.get('daemon_meta', 'runtime')
            if self.established and meta is None:
                invalid('Lost runtime journal; refusing empty recovery')
            if meta is not None and meta['target'] != self.target:
                invalid('Journal binding differs from provisioned runtime')

    def finish(self):
        private_json(self.marker, self.target)
        self.pending.unlink(missing_ok=True)
        # The fsynced established marker takes precedence even if the pending
        # unlink is lost on crash. It can never authorize empty recovery again.
