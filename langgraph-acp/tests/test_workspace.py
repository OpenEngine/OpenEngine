"""The filesystem context handed to a session."""

from pathlib import Path

import pytest

from langgraph_acp import ACPWorkspace


def test_a_workspace_defaults_to_leaving_the_choice_to_the_provider() -> None:
    assert ACPWorkspace() == ACPWorkspace(cwd=None, additional_directories=())


def test_paths_are_stored_as_strings() -> None:
    """A JSON-RPC request has no room for a `PosixPath`."""
    workspace = ACPWorkspace(
        cwd=Path("/repos/api"),
        additional_directories=[Path("/repos/docs")],
    )

    assert workspace.cwd == "/repos/api"
    assert workspace.additional_directories == ("/repos/docs",)


def test_additional_directories_are_a_tuple_however_they_arrive() -> None:
    """Resolvers return lists; a workspace is a value and stays one."""
    workspace = ACPWorkspace(additional_directories=["/repos/docs", "/repos/specs"])

    assert workspace.additional_directories == ("/repos/docs", "/repos/specs")


def test_one_path_where_a_list_of_them_belongs_is_refused() -> None:
    """`lambda state: [state["docs"]]` is one pair of brackets from shredding it.

    A `str` is iterable, so without the guard a single root becomes one entry
    per character and the session opens on a dozen directories that do not
    exist.
    """
    with pytest.raises(TypeError, match="additional_directories"):
        ACPWorkspace(cwd="/repos/api", additional_directories="/repos/docs")


def test_workspaces_compare_by_value() -> None:
    assert ACPWorkspace(cwd="/repos/api") == ACPWorkspace(cwd=Path("/repos/api"))
    assert ACPWorkspace(cwd="/repos/api") != ACPWorkspace(cwd="/repos/web")
