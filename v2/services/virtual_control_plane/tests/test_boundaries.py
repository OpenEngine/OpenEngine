"""VCP's domain/application layer stays independent of transport and storage details.

`virtual_control_plane.application` is where #1054's worktree adapter and
#1114's smolvm adapter are expected to be used from, through the ports in
`application/ports.py` -- never imported directly, which is what would force
this layer to branch on which one it holds.
"""

from __future__ import annotations

import ast
from pathlib import Path

BANNED = {"nats", "sqlalchemy", "smolvm", "fcntl"}
APPLICATION = Path(__file__).parents[1] / "application"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


def test_the_application_layer_imports_no_transport_or_storage_library() -> None:
    offenders = {
        str(path): hit
        for path in APPLICATION.rglob("*.py")
        if (hit := _imports(path) & BANNED)
    }

    assert offenders == {}
