"""Which engine runs a workflow, and what composing a graph one costs.

The question this file exists for is the second one. A graph workflow is not a
mode the interface is put into: it is a definition the repository provided, and
the same process serves the surface for it *and* everything it served before. So
what is checked here is the seam --

* a workflow directory decides which engine runs what, with no flag involved;
* a directory of step workflows composes exactly the application it always did;
* mounting the control surface leaves the interface, and its lifespan, intact.

-- plus the small contract a workflow file is written against: what
`graph_workflow` accepts, and that a node's own name reaches the topology so the
file does not have to restate it.

What a run *does* is `tests/test_graph_runtime_langgraph_acp.py`, against a real
ACP agent on a real pipe. Nothing here starts one.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from engine.apps.web import graphs
from engine.apps.web.__main__ import compose_app
from engine.graph_runtime import GraphWorkflow
from engine.graph_runtime_langgraph import (
    State,
    agent_registry,
    answer_permission,
    graph_workflow,
    sqlite_runtime,
)
from engine.runtime.config import EngineConfig, LoadedEngineConfig, WorkflowsConfig
from engine.runtime.workflows import WorkflowLoadError, load_workflow_catalog
from langgraph.graph import END, START, StateGraph
from langgraph_acp.providers import CodexACPProvider
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

#: The repository's own workflows, and the variant a test is pointed at instead.
REPOSITORY = Path(__file__).resolve().parents[1] / "workflows"
VARIANT = Path(__file__).resolve().parent / "graph_workflows"


def builder() -> StateGraph:
    async def node(_state: dict[str, object]) -> dict[str, object]:
        return {}

    graph: StateGraph = StateGraph(State)
    graph.add_node("only", node)
    graph.add_edge(START, "only")
    graph.add_edge("only", END)
    return graph


# --- which engine runs which workflow ---------------------------------------


def test_a_directory_decides_which_engine_runs_what() -> None:
    """The repository ships both kinds, and the loader keeps them apart."""
    catalog = load_workflow_catalog(REPOSITORY)

    assert [str(definition.workflow_id) for definition in catalog] == [
        "implementation-review-v1"
    ]
    assert [str(graph.graph_id) for graph in catalog.graphs] == [
        "implementation-review-codex",
        "implementation-review-claude",
    ]


def test_a_test_runs_a_variant_by_being_pointed_at_one() -> None:
    """No flag, no second mode: a different directory is a different workflow."""
    catalog = load_workflow_catalog(VARIANT)

    assert len(catalog) == 0
    assert [str(graph.graph_id) for graph in catalog.graphs] == ["tiny"]


def test_a_directory_of_step_workflows_offers_no_graphs(tmp_path: Path) -> None:
    """What tells a composition root there is nothing to compose."""
    (tmp_path / "steps.py").write_text(
        (REPOSITORY / "implementation_review.py").read_text(), encoding="utf-8"
    )

    catalog = load_workflow_catalog(tmp_path)

    assert catalog.graphs == ()
    assert len(catalog) == 1


def test_one_namespace_across_both_kinds(tmp_path: Path) -> None:
    """A person choosing something to run does not care which engine is behind it."""
    (tmp_path / "graph.py").write_text(
        "from engine.graph_runtime_langgraph import State, graph_workflow\n"
        "from langgraph.graph import END, START, StateGraph\n"
        "async def only(state):\n"
        "    return {}\n"
        "@graph_workflow(id='implementation-review-v1', name='Collides')\n"
        "def workflow():\n"
        "    builder = StateGraph(State)\n"
        "    builder.add_node('only', only)\n"
        "    builder.add_edge(START, 'only')\n"
        "    builder.add_edge('only', END)\n"
        "    return builder\n",
        encoding="utf-8",
    )
    (tmp_path / "steps.py").write_text(
        (REPOSITORY / "implementation_review.py").read_text(), encoding="utf-8"
    )

    with pytest.raises(WorkflowLoadError, match="duplicate workflow id"):
        load_workflow_catalog(tmp_path)


def test_a_module_exporting_something_else_is_refused(tmp_path: Path) -> None:
    (tmp_path / "nonsense.py").write_text("workflow = 3\n", encoding="utf-8")

    with pytest.raises(WorkflowLoadError, match="neither an openengine workflow"):
        load_workflow_catalog(tmp_path)


# --- what a workflow file is written against --------------------------------


def test_a_graph_workflow_is_named_the_same_way_either_way() -> None:
    """A file describing one graph decorates; one describing a family calls."""

    @graph_workflow(id="decorated", name="Decorated")
    def decorated() -> StateGraph:
        return builder()

    called = graph_workflow(builder(), id="called", name="Called")

    assert isinstance(decorated, GraphWorkflow)
    assert isinstance(called, GraphWorkflow)
    assert (str(decorated.graph_id), decorated.name) == ("decorated", "Decorated")
    assert (str(called.graph_id), called.name) == ("called", "Called")


def test_a_graph_workflow_refuses_what_it_cannot_run() -> None:
    with pytest.raises(ValueError, match="needs an id"):
        graph_workflow(builder(), id=" ", name="Named")
    with pytest.raises(ValueError, match="needs a name"):
        graph_workflow(builder(), id="named", name="")
    with pytest.raises(TypeError, match="must be built from a StateGraph"):
        graph_workflow("not a graph", id="named", name="Named")


def test_agents_answer_permission_requests_rather_than_declining_them() -> None:
    """The wiring a workflow file would get silently wrong.

    A provider left with the default handler refuses every request instead of
    asking anybody, and the run would look like an agent that changed its mind.
    """
    registry = agent_registry([CodexACPProvider()])

    assert registry.resolve("codex").permissions is answer_permission


def test_a_node_names_itself_so_the_workflow_need_not(tmp_path: Path) -> None:
    """Read off the compiled graph, which is why a name cannot go stale."""
    catalog = load_workflow_catalog(REPOSITORY)

    topologies = asyncio.run(_topologies(catalog.graphs, tmp_path))
    codex = next(one for one in topologies if str(one.graph_id).endswith("codex"))

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
    # The kinds a client draws differently: a checkout, two agents, and the one
    # stage that is a person.
    assert [node.kind for node in codex.nodes] == [
        "workspace",
        "agent",
        "agent",
        "human",
    ]
    assert str(codex.entry_point) == "workspace"


def test_a_runtime_opens_durable_files_and_closes_them_after(tmp_path: Path) -> None:
    """The reason this is a context manager and not a factory.

    Both files, because they answer to two owners -- LangGraph's idea of where a
    run stands, and the runtime's own record of outstanding approvals and agent
    sessions -- and both shut, so the next process to open them is not racing
    the one that made them.
    """
    catalog = load_workflow_catalog(VARIANT)
    state = tmp_path / "state"

    async def scenario() -> tuple[list[str], object]:
        async with sqlite_runtime(catalog.graphs, state) as runtime:
            assert [str(one.graph_id) for one in runtime.graphs()] == ["tiny"]
            return sorted(path.name for path in state.iterdir()), runtime

    written, runtime = asyncio.run(scenario())

    assert written == ["checkpoints.sqlite3", "graph-runs.sqlite3"]
    assert runtime.running() == ()
    with pytest.raises(sqlite3.ProgrammingError):
        asyncio.run(runtime.store.runs())


def test_a_runtime_needs_something_to_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with sqlite_runtime((), tmp_path / "state"):
            pass

    with pytest.raises(ValueError, match="at least one workflow"):
        asyncio.run(scenario())


# --- composing the two surfaces together ------------------------------------


def test_a_deployment_with_no_graph_workflow_composes_what_it_always_did(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "steps").mkdir()
    (tmp_path / "steps" / "steps.py").write_text(
        (REPOSITORY / "implementation_review.py").read_text(), encoding="utf-8"
    )
    catalog = load_workflow_catalog(tmp_path / "steps")

    app = compose_app(_configured(tmp_path / "steps"), catalog)
    answers = asyncio.run(_ask(app))

    assert answers["config"].status_code == 200
    assert answers["graphs"].status_code == 404


def test_a_graph_workflow_composes_its_control_surface_beside_the_interface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    catalog = load_workflow_catalog(VARIANT)

    app = compose_app(_configured(VARIANT), catalog)
    answers = asyncio.run(_ask(app))

    assert answers["config"].status_code == 200
    assert answers["graphs"].status_code == 200
    assert [one["graphId"] for one in answers["graphs"].json()["graphs"]] == ["tiny"]
    # Nothing of the graph runtime's leaks onto the interface's own paths: both
    # servers spell a run `/api/runs`, which is why one of them is mounted.
    assert answers["unprefixed"].status_code == 404


def test_mounting_the_control_surface_still_runs_the_interfaces_lifespan(
    tmp_path: Path,
) -> None:
    """Not the parent's by default, and the interface restores its runs in one."""
    started: list[str] = []

    @asynccontextmanager
    async def interface_lifespan(_app: Starlette) -> AsyncIterator[None]:
        started.append("interface")
        yield

    interface = Starlette(
        lifespan=interface_lifespan,
        routes=[Route("/api/config", lambda _r: JSONResponse({"agents": []}))],
    )
    catalog = load_workflow_catalog(VARIANT)

    combined = graphs.serve(interface, catalog.graphs, tmp_path / "state")
    answers = asyncio.run(_ask(combined))

    assert started == ["interface"]
    assert answers["config"].json() == {"agents": []}
    assert answers["graphs"].status_code == 200


def test_a_graph_this_binding_cannot_run_is_refused_before_the_server_starts(
    tmp_path: Path,
) -> None:
    """The catalog's contract is an id and a name; running one needs more."""

    class Foreign:
        graph_id = "foreign"
        name = "Somebody else's engine"

    with pytest.raises(TypeError, match="LangGraph binding"):
        graphs.serve(Starlette(), [Foreign()], tmp_path / "state")


def _configured(directory: Path) -> LoadedEngineConfig:
    return LoadedEngineConfig(
        config=EngineConfig(workflows=WorkflowsConfig(directory=str(directory)))
    )


async def _topologies(workflows: object, tmp_path: Path) -> tuple:
    async with sqlite_runtime(workflows, tmp_path / "state") as runtime:  # type: ignore[arg-type]
        return runtime.graphs()


async def _ask(app: Starlette) -> dict[str, httpx.Response]:
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return {
                "config": await client.get("/api/config"),
                "graphs": await client.get(f"{graphs.PREFIX}/api/graphs"),
                "unprefixed": await client.get("/api/graphs"),
            }
