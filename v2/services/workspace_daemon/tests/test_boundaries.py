"""The daemon is a separate process: it shares contracts and messaging with VCP, never VCP's code."""

from __future__ import annotations

import ast
from pathlib import Path

DAEMON = Path(__file__).parents[1]


def test_the_daemon_imports_nothing_from_vcp() -> None:
    offenders = set()
    for path in DAEMON.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            offenders.update(f"{path.name}: {name}" for name in names if name.startswith("virtual_control_plane"))

    assert offenders == set()
