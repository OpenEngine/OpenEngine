"""The virtual control plane's domain and application layer.

Nothing under this package may import NATS, SQLAlchemy, smolvm or any other
host- or transport-specific module: `tests/test_boundaries.py` enforces it.
Composition adapters (`virtual_control_plane/__main__.py`, `persistence/`,
future provider packages) depend on this layer, never the other way around.
"""

from __future__ import annotations
