"""VCP's domain/application layer stays independent of transport, storage and provider details.

`virtual_control_plane.application` drives the smolvm adapter in
`providers/smolvm` (and #1054's worktree adapter) only through the ports in
`application/ports.py` -- never by importing one, which is what would force
this layer to branch on which one it holds, or let whole-vCPU and image rules
leak into every workspace.
"""

from __future__ import annotations

import ast
from pathlib import Path

BANNED = {"nats", "sqlalchemy", "smolvm", "fcntl"}
BANNED_PACKAGES = ("virtual_control_plane.providers",)
APPLICATION = Path(__file__).parents[1] / "application"


def _imports(path: Path) -> set[str]:
    """Every imported module, by full dotted name."""
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def _banned(modules: set[str]) -> set[str]:
    return {module for module in modules
            if module.split(".")[0] in BANNED or module.startswith(BANNED_PACKAGES)}


def test_the_application_layer_imports_no_transport_storage_library_or_provider() -> None:
    offenders = {
        str(path): hit
        for path in APPLICATION.rglob("*.py")
        if (hit := _banned(_imports(path)))
    }

    assert offenders == {}


def test_the_smolvm_adapter_keeps_its_guest_layout_and_cli_to_itself() -> None:
    """Guest paths and the machine CLI are the adapter's business: nothing in
    `application/` names them, so they never become universal workspace rules."""
    sources = "\n".join(path.read_text() for path in APPLICATION.rglob("*.py"))

    for detail in ("/workspace", "machine exec", "oe-vcp-", "--cpus", "microvm"):
        assert detail not in sources, detail
