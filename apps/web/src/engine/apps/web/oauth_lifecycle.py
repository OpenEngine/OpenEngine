"""Safe, structured observability for OAuth credential lifecycle events."""

from __future__ import annotations

import hashlib
import json
import logging
import os

_LOG = logging.getLogger(__name__)


def token_fingerprint(token: str | None) -> str | None:
    """Return a stable identifier for a token without logging token material."""
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def oauth_lifecycle_event(event: str, /, **details: str | float | None) -> None:
    """Emit one JSON log record containing only non-secret OAuth metadata."""
    _LOG.info(
        "oauth_lifecycle %s",
        json.dumps({"event": event, "pid": os.getpid(), **details}, sort_keys=True),
    )
