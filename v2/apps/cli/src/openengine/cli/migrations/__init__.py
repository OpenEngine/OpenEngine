"""What `oe migrate` applies: the services' databases, then NATS's streams."""

from __future__ import annotations


class MigrationError(RuntimeError):
    pass
