"""Tests for the boundary checker itself.

A boundary test that silently fails to parse an import is worse than no test at
all -- it reports green while the wall it guards has a hole. An earlier version
of `_resolve_relative` resolved `from ..adapters.github import X` inside
`engine/runtime/dispatcher.py` to `engine.runtime.adapters.github`, which
matched no rule and so passed. These pin the parsing down directly.
"""

import ast
import textwrap
from pathlib import Path

import pytest

from layout import imported_modules, is_first_party, is_stdlib


def _write(tmp_path: Path, rel: str, source: str) -> Path:
    path = tmp_path / "src" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source))
    return path


def test_absolute_imports_are_collected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "engine/runtime/dispatcher.py",
        """
        import os
        from engine.domain.commands import Command
        from engine.ports import StateStore
        """,
    )
    assert imported_modules(path) == {"os", "engine.domain.commands", "engine.ports"}


@pytest.mark.parametrize(
    "rel,statement,expected",
    [
        # From a module: level 1 is its own package.
        ("engine/runtime/dispatcher.py", "from . import capabilities", "engine.runtime"),
        ("engine/runtime/dispatcher.py", "from .capabilities import C", "engine.runtime.capabilities"),
        # Level 2 climbs to `engine` -- the escape that used to slip through.
        ("engine/runtime/dispatcher.py", "from ..adapters.github import G", "engine.adapters.github"),
        ("engine/runtime/dispatcher.py", "from ..domain import Command", "engine.domain"),
        # From a package's __init__.py: the file *is* the package.
        ("engine/runtime/__init__.py", "from .dispatcher import D", "engine.runtime.dispatcher"),
        ("engine/runtime/__init__.py", "from ..adapters.openai import C", "engine.adapters.openai"),
        # Nested one deeper.
        ("engine/adapters/github/__init__.py", "from ..openai import C", "engine.adapters.openai"),
    ],
)
def test_relative_imports_resolve_to_absolute(
    tmp_path: Path, rel: str, statement: str, expected: str
) -> None:
    path = _write(tmp_path, rel, statement + "\n")
    assert imported_modules(path) == {expected}


def test_imports_inside_functions_are_still_seen(tmp_path: Path) -> None:
    """Deferred imports are the classic way to sneak past a layering rule."""
    path = _write(
        tmp_path,
        "engine/core/decide.py",
        """
        def decide(state, event):
            from engine.adapters.github import GitHubSourceControl
            return state
        """,
    )
    assert imported_modules(path) == {"engine.adapters.github"}


def test_imports_inside_type_checking_blocks_are_still_seen(tmp_path: Path) -> None:
    """`if TYPE_CHECKING:` hides an import from the interpreter, not from the
    architecture -- a core module still names an adapter in its types."""
    path = _write(
        tmp_path,
        "engine/core/decide.py",
        """
        from typing import TYPE_CHECKING

        if TYPE_CHECKING:
            from engine.adapters.postgres import PostgresStateStore
        """,
    )
    assert "engine.adapters.postgres" in imported_modules(path)


def test_syntax_error_is_not_silently_swallowed(tmp_path: Path) -> None:
    path = _write(tmp_path, "engine/core/broken.py", "def (\n")
    with pytest.raises(SyntaxError):
        imported_modules(path)


@pytest.mark.parametrize("module", ["os", "sys", "typing", "dataclasses", "tomllib", "asyncio"])
def test_stdlib_is_recognised(module: str) -> None:
    assert is_stdlib(module)


@pytest.mark.parametrize("module", ["requests", "temporalio", "httpx", "psycopg"])
def test_third_party_is_not_stdlib(module: str) -> None:
    assert not is_stdlib(module)
    assert not is_first_party(module)


@pytest.mark.parametrize("module", ["engine", "engine.domain", "engine.adapters.github"])
def test_first_party_is_recognised(module: str) -> None:
    assert is_first_party(module)


def test_lookalike_package_is_not_first_party() -> None:
    """`engineering` must not be mistaken for our namespace."""
    assert not is_first_party("engineering")
    assert not is_first_party("engine_extras")
