"""Structured event logging for the runtime.

The repository has no third-party observability stack, so this is the one
small seam every process can import without pulling in a dependency: a single
JSON record per event, written to stderr with a constant message template and
every dynamic value in a named field. Observability tooling clusters events by
message; interpolating values into the message would fragment those clusters.

Records carry a `level` field so they stay one line and are filterable without
a logger-formatter round trip.
"""

from __future__ import annotations

import json
import sys
from typing import Any


def log_event(
    message: str,
    *,
    level: str = "info",
    **fields: Any,
) -> None:
    """Emit one structured event.

    `message` is a constant template; every dynamic value belongs in `fields`.
    Fields are JSON-encoded with no fallback serializer, so only already-safe
    scalars (str, int, float, bool, None) may be forwarded: opaque objects and
    exception objects raise instead of being stringified. Callers pass an
    enumerated `error_type` (the exception class name) rather than the
    exception itself, so a message or traceback that might embed payloads never
    reaches the log.
    """
    record: dict[str, Any] = {"level": level, "event": message, **fields}
    print(json.dumps(record, separators=(",", ":")), file=sys.stderr)


__all__ = ["log_event"]