"""A repository holding a graph workflow nothing runs yet.

Two things can go wrong with adding `workflows/implementation_review_graph.py`:

* the definition itself is wrong, and nothing notices, because nothing runs it;
* it breaks something that reads the workflow directory -- which is every app
  here. A directory that refuses to load takes the deployment down, and a graph
  in the workflow list would be offered to somebody who cannot be given one.

Most of this file is about the second. The boot tests are not ceremony: before
this change the loader raised on an export it did not recognise, and reading a
workflow file means importing what that file imports.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import httpx
import pytest

from engine.adapters.workspace_provider.git_worktree import (
    DEFAULT_ROOT_DIRECTORY,
    GitWorktreeWorkspaceProvider,
)
from engine.apps.control_server.__main__ import main as control_server
from engine.apps.control_server.composition import Settings as ControlServerSettings
from engine.apps.web.__main__ import build_app
from engine.apps.web.composition import Settings
from engine.apps.worker.__main__ import main as worker
from engine.apps.worker.composition import Settings as WorkerSettings
from engine.domain import WorkflowId, WorkspaceId
from engine.graph_runtime import GraphWorkflow
from engine.graph_runtime_langgraph.components import HumanReviewNode
from engine.graph_runtime_langgraph.workflows import sqlite_runtime
from engine.ports import Workspace
from engine.runtime.workflows import load_workflow_catalog

#: The repository's own workflow directory: what a deployment here actually
#: loads, rather than a fixture shaped like one.
WORKFLOWS = Path(__file__).resolve().parents[1] / "workflows"

#: Started by every composition root under test, and by the interface.
CONFIG = Path(__file__).resolve().parents[1] / "engine.toml"

STARTABLE = "implementation-review-v1"
GRAPHS = ("implementation-review-codex", "implementation-review-claude")


class RecordingWorkspaceProvider:
    """A `WorkspaceProvider` that provisions nothing.

    Enough of the port to be handed to a node, and no more: these tests ask
    which provider a node holds, never what it produced.
    """

    async def provision(self, repository: str, base_ref: str) -> Workspace:
        return Workspace(
            workspace_id=WorkspaceId("ws-recorded"),
            root_path="/checkouts/ws-recorded",
            repository=repository,
            base_ref=base_ref,
            ref="engine/ws-recorded",
        )


def catalog():
    return load_workflow_catalog(WORKFLOWS)


def definition_module():
    """The workflow module, imported by path the way the loader imports one.

    By path because `workflows/` is a directory of definitions, not a package.
    That is the point of it: a deployment swaps the directory, not an import.
    """
    path = WORKFLOWS / "implementation_review_graph.py"
    spec = importlib.util.spec_from_file_location("_implementation_review_graph", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def nodes_of(builder) -> dict[str, object]:
    """The node objects a `StateGraph` was built from, before compilation.

    The one place these tests reach into LangGraph's own structure, kept to one
    function so a change in its shape is one fix. Worth reaching for: `cwd` is
    not in the topology a client sees, and it is the field whose absence would
    put an agent in the server's own repository.
    """
    return {
        name: spec.runnable.afunc or spec.runnable.func
        for name, spec in builder.nodes.items()
    }


# --- the definition ----------------------------------------------------------


def test_the_repository_offers_the_same_workflow_on_either_engine() -> None:
    loaded = catalog()

    assert [str(one.graph_id) for one in loaded.graphs] == list(GRAPHS)
    assert [one.name for one in loaded.graphs] == [
        "Implementation review (codex)",
        "Implementation review (claude)",
    ]
    # One per runner, because an agent node names the agent it runs. Choosing a
    # runner means choosing a graph, not filling in a field on one.
    assert all(isinstance(one, GraphWorkflow) for one in loaded.graphs)


def test_the_graph_is_the_four_stages_the_step_version_describes(
    tmp_path: Path,
) -> None:
    """Read off the compiled graph, so a stage cannot be renamed by accident.

    Compiling is the only way to see the topology in a repository that cannot
    yet run one -- and worth the trouble, because a definition nothing runs is
    exactly the kind that rots unnoticed.
    """

    async def scenario():
        async with sqlite_runtime(catalog().graphs, tmp_path / "state") as runtime:
            return runtime.graphs()

    topologies = asyncio.run(scenario())
    codex = next(one for one in topologies if str(one.graph_id) == GRAPHS[0])

    assert [str(node.node_id) for node in codex.nodes] == [
        "workspace",
        "implementation",
        "review",
        "human-review",
    ]
    assert [node.name for node in codex.nodes] == [
        "Workspace",
        "Implementation",
        "Review",
        "Human review",
    ]
    # The kinds a client would draw differently: a checkout, two agents, and
    # the one stage that is a person.
    assert [node.kind for node in codex.nodes] == [
        "workspace",
        "agent",
        "agent",
        "human",
    ]
    assert str(codex.entry_point) == "workspace"


def test_a_variant_replaces_where_it_works_by_calling_rather_than_copying() -> None:
    """The check that keeps one copy of this workflow.

    Something that needs its checkouts elsewhere -- the browser tier, whose
    whole premise is that a run's worktrees belong to the test -- can either
    call `pipeline` with its own provider or paste the graph into a second file
    and change one line. Only the first is available if this argument exists.

    Asserted on the provider the node ends up holding, because the failure
    worth catching is a `pipeline` that takes the argument and builds its
    default anyway.
    """
    module = definition_module()
    mine = RecordingWorkspaceProvider()

    nodes = nodes_of(module.pipeline("codex", workspace_provider=mine))

    assert nodes[module.WORKSPACE].provider is mine
    # And the default is still a real provider, for the deployment that passes
    # nothing. An accidental `None` would be a run with nowhere to work.
    default = nodes_of(module.pipeline("codex"))[module.WORKSPACE].provider
    assert isinstance(default, GitWorktreeWorkspaceProvider)


def test_the_default_root_is_the_one_every_composition_root_uses() -> None:
    """One string, not a fifth copy of it.

    Three apps already honour `Settings.workspace_root`. A workflow restating
    the path beside them is a deployment whose two halves disagree about where
    its worktrees are, with both halves in the same process.
    """
    assert Settings().workspace_root == DEFAULT_ROOT_DIRECTORY
    assert WorkerSettings().workspace_root == DEFAULT_ROOT_DIRECTORY
    assert ControlServerSettings().workspace_root == DEFAULT_ROOT_DIRECTORY
    assert DEFAULT_ROOT_DIRECTORY not in (
        WORKFLOWS / "implementation_review_graph.py"
    ).read_text(encoding="utf-8")


def test_every_agent_node_works_in_the_run_s_own_checkout() -> None:
    """The property that would be worst to get wrong.

    An agent node given no working directory opens its session in the server's
    own repository. `NoWorkingDirectoryError` makes that impossible to reach
    quietly; this checks the definition never has to. Written over *every*
    agent node, so it still means something when a third is added.
    """
    module = definition_module()
    nodes = nodes_of(module.pipeline("codex"))
    agents = [
        node
        for node in nodes.values()
        if getattr(node, "graph_node_kind", "") == "agent"
    ]

    assert len(agents) == 2
    assert all(node.cwd is module.checkout for node in agents)
    # And something upstream of them actually provisions one.
    assert nodes["workspace"].graph_node_kind == "workspace"


def test_the_human_stage_is_the_shared_component_rather_than_a_bespoke_node() -> None:
    """A person's verdict, raised the way everything expects to find it.

    A verdict and an agent asking permission both arrive as approvals on the
    same feed, and `HumanReviewNode`'s tool name is what tells them apart. A
    hand-rolled stopping point would be a pause no client could label.
    """
    human = nodes_of(definition_module().pipeline("codex"))["human-review"]

    assert isinstance(human, HumanReviewNode)
    assert human.graph_node_kind == "human"


# --- nothing offers it -------------------------------------------------------


def test_a_graph_workflow_is_not_something_a_person_can_be_offered() -> None:
    """Kept out of the list by the catalog, not by a filter in a client.

    Everything that lists workflows reads this iteration, so the exclusion
    lives in one place instead of one per surface that grows a dropdown.
    """
    loaded = catalog()

    assert [str(one.workflow_id) for one in loaded] == [STARTABLE]
    assert len(loaded) == 1
    for graph_id in GRAPHS:
        assert WorkflowId(graph_id) not in loaded
        assert loaded.get(WorkflowId(graph_id)) is None


def test_the_interface_offers_only_what_it_can_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dropdown itself, through the endpoint the client reads it from.

    At `/api/config` rather than on the catalog a second time, because the
    question is about the interface: a workflow nothing here can start must not
    be something somebody can pick and watch fail.
    """
    monkeypatch.setenv("ENGINE_CONFIG", str(CONFIG))
    monkeypatch.chdir(tmp_path)
    app = build_app()

    async def ask() -> dict:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            answered = await client.get("/api/config")
            assert answered.status_code == 200
            return answered.json()

    offered = asyncio.run(ask())["workflows"]

    assert [one["id"] for one in offered] == [STARTABLE]


# --- and nothing falls over --------------------------------------------------


def test_every_composition_root_still_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All three read the workflow directory at startup, so all three are here.

    One test, two failure modes: the loader refusing an export it did not
    recognise, and an app that cannot import what the workflow file imports.
    """
    monkeypatch.chdir(tmp_path)

    assert worker(["--config", str(CONFIG)]) == 0
    assert control_server(["--config", str(CONFIG)]) == 0
    # The interface has no exit code to check. Building the app is what
    # `engine-web` does before it serves anything, so building it is the test.
    monkeypatch.setenv("ENGINE_CONFIG", str(CONFIG))
    assert build_app() is not None
