"""Shape command results for the terminal or for scripts."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [max(len(str(cell)) for cell in column) for column in zip(headers, *rows)]
    lines = [headers, *rows]
    return "\n".join("  ".join(str(cell).ljust(width) for cell, width in zip(line, widths)).rstrip() for line in lines)


def as_json(value: Any) -> str:
    return json.dumps(value, indent=2)
