"""Managed provider adapters behind `application/ports.py`'s `AllocationProvider`.

Only the service entrypoint and tests import from here. The application layer
never does: `tests/test_boundaries.py` enforces that no module under
`application/` imports a provider, so image names, guest paths, VM resource
rules and CLI details stay inside the adapter that owns them.
"""
