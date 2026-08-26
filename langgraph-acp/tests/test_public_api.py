"""The package imports, exports what it says it does, and depends on nothing.

The last of those is the acceptance criterion that is easiest to lose later.
The ACP client is stdlib asyncio and JSON-RPC written here rather than a
dependency, precisely so that using an ACP agent from LangGraph does not drag a
second protocol library into every application that installs this one. These
checks are static analysis over the source tree, so an import that only
executes on one branch still fails them.
"""

import ast
import importlib
import sys
import tomllib
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
SRC = PACKAGE_ROOT / "src" / "langgraph_acp"
SOURCE_FILES = sorted(SRC.rglob("*.py"))

#: The modules that make up the package's public surface.
PUBLIC_MODULES = (
    "langgraph_acp.agent",
    "langgraph_acp.client",
    "langgraph_acp.config",
    "langgraph_acp.errors",
    "langgraph_acp.events",
    "langgraph_acp.providers",
    "langgraph_acp.providers.codex",
    "langgraph_acp.result",
    "langgraph_acp.session",
    "langgraph_acp.workspace",
)


def imported_modules(path: Path) -> set[str]:
    """Every module a source file imports, as an absolute dotted name."""
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # Relative imports resolve within the package, so they are first
            # party by construction and nothing here needs to unpick them.
            if node.level == 0 and node.module:
                found.add(node.module)
    return found


def test_the_package_imports() -> None:
    assert importlib.import_module("langgraph_acp").__all__


def test_every_exported_name_resolves() -> None:
    package = importlib.import_module("langgraph_acp")
    missing = [name for name in package.__all__ if not hasattr(package, name)]
    assert not missing, f"exported but absent: {missing}"


def test_exports_are_sorted() -> None:
    package = importlib.import_module("langgraph_acp")
    assert list(package.__all__) == sorted(package.__all__)


@pytest.mark.parametrize("module_name", PUBLIC_MODULES)
def test_each_module_reexports_its_public_names(module_name: str) -> None:
    """Nothing a module declares public is unreachable from the package."""
    package = importlib.import_module("langgraph_acp")
    module = importlib.import_module(module_name)
    unexported = [name for name in module.__all__ if name not in package.__all__]
    assert not unexported, f"{module_name} declares {unexported}, package hides them"


@pytest.mark.parametrize("path", SOURCE_FILES, ids=lambda p: p.name)
def test_no_third_party_imports(path: Path) -> None:
    """Only the standard library and this package. No ACP client, no LangGraph.

    The core types describe a conversation with an agent; naming one here would
    make the vocabulary unusable for the next agent that speaks ACP.
    """
    external = {
        module
        for module in imported_modules(path)
        if module.split(".")[0] not in sys.stdlib_module_names
        and not module.startswith("langgraph_acp")
    }
    assert not external, f"{path.name} imports {sorted(external)}"


def test_the_distribution_declares_no_dependencies() -> None:
    """The invariant above, in the form a packaging tool can also see."""
    pyproject = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["dependencies"] == []
