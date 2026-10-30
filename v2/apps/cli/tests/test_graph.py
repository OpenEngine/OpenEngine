"""`oe graph list`, `get` and `apply`: against a real NATS and GE, and when OpenEngine cannot answer, an actionable message and a nonzero exit."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from openengine.cli.commands.graph import BadReference, parse_reference, project, render, render_revision
from openengine.cli.config.local import cache_directory
from openengine.supervisor.processes import nats
from openengine.supervisor.readiness.http import wait_until_healthy
from openengine.supervisor.state import ProcessState, Status, SupervisorState, state_path, write_state

ADVICE = "Start it with `oe up`; see `oe logs`."


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


EXAMPLE = Path(__file__).parents[3] / "packages/contracts/graph_engine/v1/resources/examples/implement-review.graph.yaml"


def _graph_list(state_home: Path, *arguments: str, **env: str) -> subprocess.CompletedProcess[str]:
    environment = {key: value for key, value in os.environ.items() if key != "OE_PROJECT"}
    return subprocess.run(
        [sys.executable, "-m", "openengine.cli", "graph", *(arguments or ["list"])],
        env={**environment, "XDG_STATE_HOME": str(state_home), **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


def _recorded(state_home: Path, nats_url: str) -> None:
    """A supervisor state naming `nats_url`, recorded under this live test process's pid."""
    write_state(
        state_path(state_home / "openengine"),
        SupervisorState(pid=os.getpid(), status=Status.READY, processes=[ProcessState("nats", 0, nats_url, Status.READY)]),
    )


def test_without_a_supervisor(tmp_path: Path) -> None:
    result = _graph_list(tmp_path)

    assert result.returncode == 1
    assert result.stderr.strip() == f"OpenEngine is not running (no supervisor). {ADVICE}"
    assert result.stdout == ""


def test_apply_without_a_supervisor(tmp_path: Path) -> None:
    result = _graph_list(tmp_path, "apply", str(EXAMPLE))

    assert result.returncode == 1
    assert result.stderr.strip() == f"OpenEngine is not running (no supervisor). {ADVICE}"


def test_apply_checks_the_file_before_sending_it(tmp_path: Path) -> None:
    graph = tmp_path / "graph.yaml"
    graph.write_text(EXAMPLE.read_text().replace("kind: Graph", "kind: Loop"))

    result = _graph_list(tmp_path, "apply", str(graph))

    assert result.returncode == 1
    assert result.stderr.strip() == f"{graph}: kind: is a Loop, not a Graph"
    assert result.stdout == ""


def test_when_nats_cannot_be_reached(tmp_path: Path) -> None:
    url = f"nats://127.0.0.1:{_free_port()}"
    _recorded(tmp_path, url)

    result = _graph_list(tmp_path)

    assert result.returncode == 1
    assert result.stderr.strip() == f"OpenEngine is not running (cannot reach NATS at {url}). {ADVICE}"


@pytest.mark.integration
def test_when_nats_runs_but_ge_does_not(tmp_path: Path) -> None:
    port, monitor_port = _free_port(), _free_port()
    command = nats.server_command(
        nats.install_nats_server(cache_directory()), host="127.0.0.1", port=port,
        monitor_port=monitor_port, store=tmp_path / "nats",
    )
    server = nats.start(command, subprocess.DEVNULL)
    try:
        wait_until_healthy("nats-server", server, f"http://127.0.0.1:{monitor_port}/healthz", timeout=30)
        _recorded(tmp_path, f"nats://127.0.0.1:{port}")

        result = _graph_list(tmp_path)
    finally:
        server.terminate()
        server.wait()

    assert result.returncode == 1
    assert result.stderr.strip() == f"OpenEngine is not running (no reply from ge). {ADVICE}"


def test_get_without_a_supervisor(tmp_path: Path) -> None:
    result = _graph_list(tmp_path, "get", "implement-review")

    assert result.returncode == 1
    assert result.stderr.strip() == f"OpenEngine is not running (no supervisor). {ADVICE}"


def test_a_reference_is_a_name_and_an_optional_revision(tmp_path: Path) -> None:
    assert parse_reference("implement-review") == ("implement-review", None)
    assert parse_reference("implement-review@12") == ("implement-review", 12)
    for bad in ("review@", "review@0", "review@-1", "review@latest", "@2"):
        with pytest.raises(BadReference):
            parse_reference(bad)

    result = _graph_list(tmp_path, "get", "review@latest")
    assert result.returncode == 1
    assert result.stderr.strip() == "oe graph get: 'review@latest' is not NAME or NAME@REVISION with a revision from 1"


def test_list_and_revision_render_for_people() -> None:
    graph = {"graph_id": "graph_1", "project": "default", "name": "review", "latest_revision": 3,
             "created_at": "2026-10-01T00:00:00.000000Z", "updated_at": "2026-10-02T00:00:00.000000Z"}
    assert render([]) == "No graphs registered."
    assert render([graph]).splitlines() == [
        "PROJECT  NAME    GRAPH ID  REVISION  UPDATED",
        "default  review  graph_1   3         2026-10-02T00:00:00.000000Z",
    ]
    revision = {"graph_id": "graph_1", "revision": 2, "format": "resource", "source_sha256": "ab" * 32,
                "inputs": {}, "nodes": [], "created_at": "2026-10-01T00:00:00.000000Z",
                "source": '{"kind":"Graph","spec":{"prompt":"one\\ntwo"}}'}
    assert render_revision({"graph": graph, "revision": revision}).splitlines() == [
        "review revision 2 of 3 in project default (graph graph_1)",
        "format   resource",
        f"digest   sha256:{'ab' * 32}",
        "created  2026-10-01T00:00:00.000000Z",
        "",
        "kind: Graph",
        "spec:",
        "  prompt: |-",
        "    one",
        "    two",
    ]


def test_a_graph_without_a_project_renders_in_the_default_project() -> None:
    graph = {"graph_id": "graph_1", "name": "review", "latest_revision": 1,
             "created_at": "2026-10-01T00:00:00.000000Z", "updated_at": "2026-10-01T00:00:00.000000Z"}
    assert render([graph]).splitlines()[1].split()[:3] == ["default", "review", "graph_1"]
    revision = {"graph_id": "graph_1", "revision": 1, "format": "resource", "source_sha256": "ab" * 32,
                "inputs": {}, "nodes": [], "created_at": "2026-10-01T00:00:00.000000Z"}
    assert render_revision({"graph": graph, "revision": revision}).splitlines()[0] == (
        "review revision 1 of 1 in project default (graph graph_1)")


def test_the_project_is_the_flag_then_oe_project_then_default(monkeypatch) -> None:
    monkeypatch.delenv("OE_PROJECT", raising=False)
    assert project(argparse.Namespace(project=None)) == "default"
    monkeypatch.setenv("OE_PROJECT", "beta")
    assert project(argparse.Namespace(project=None)) == "beta"
    assert project(argparse.Namespace(project="alpha")) == "alpha"


@pytest.fixture(scope="module")
def serving(tmp_path_factory: pytest.TempPathFactory):
    """A real NATS and GE, recorded as a running supervisor's, with the example applied and then changed."""
    home = tmp_path_factory.mktemp("serving")
    port, monitor_port = _free_port(), _free_port()
    command = nats.server_command(
        nats.install_nats_server(cache_directory()), host="127.0.0.1", port=port,
        monitor_port=monitor_port, store=home / "nats",
    )
    server = nats.start(command, subprocess.DEVNULL)
    ready = home / "ge.ready"
    ge = subprocess.Popen(
        [sys.executable, "-m", "graph_engine"],
        env={**os.environ, "XDG_STATE_HOME": str(home), "OE_NATS_URL": f"nats://127.0.0.1:{port}",
             "OE_GE_READY_FILE": str(ready)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        wait_until_healthy("nats-server", server, f"http://127.0.0.1:{monitor_port}/healthz", timeout=30)
        deadline = time.monotonic() + 30
        while not ready.exists() and time.monotonic() < deadline and ge.poll() is None:
            time.sleep(0.05)
        wait_until_healthy("ge", ge, ready.read_text(), timeout=30)
        _recorded(home, f"nats://127.0.0.1:{port}")
        changed = home / "changed.graph.yaml"
        changed.write_text(EXAMPLE.read_text().replace("description: Implement an issue", "description: Changed. Implement an issue"))
        created = _graph_list(home, "apply", str(EXAMPLE))
        assert created.returncode == 0, created.stderr
        assert _graph_list(home, "apply", str(changed)).stdout.startswith("updated implement-review revision 2 in project default ")
        yield home, created.stdout.split("(graph ")[1].rstrip(")\n")
    finally:
        for process in (ge, server):
            process.terminate()
            process.wait()


@pytest.mark.integration
def test_list_shows_each_graph_and_its_latest_revision(serving) -> None:
    home, graph_id = serving

    listed = _graph_list(home, "list")
    assert listed.returncode == 0, listed.stderr
    header, row = listed.stdout.splitlines()
    assert header.split() == ["PROJECT", "NAME", "GRAPH", "ID", "REVISION", "UPDATED"]
    assert row.split()[:4] == ["default", "implement-review", graph_id, "2"]

    reply = json.loads(_graph_list(home, "list", "--json").stdout)
    assert reply["status"] == "ok"
    assert reply["result"]["next_cursor"] is None
    assert [(graph["name"], graph["graph_id"], graph["latest_revision"]) for graph in reply["result"]["items"]] == [
        ("implement-review", graph_id, 2),
    ]


@pytest.mark.integration
def test_get_shows_the_latest_or_a_named_revision(serving) -> None:
    home, graph_id = serving

    latest = _graph_list(home, "get", "implement-review")
    assert latest.returncode == 0, latest.stderr
    lines = latest.stdout.splitlines()
    assert lines[0] == f"implement-review revision 2 of 2 in project default (graph {graph_id})"
    assert lines[1] == "format   resource"
    assert "description: Changed. Implement an issue" in latest.stdout

    first = _graph_list(home, "get", "implement-review@1")
    assert first.stdout.splitlines()[0] == f"implement-review revision 1 of 2 in project default (graph {graph_id})"
    assert "Changed." not in first.stdout
    assert first.stdout.splitlines()[2] != lines[2]  # another digest

    reply = json.loads(_graph_list(home, "get", "implement-review@1", "--json").stdout)
    assert reply["result"]["revision"]["revision"] == 1
    assert reply["result"]["graph"]["graph_id"] == graph_id


@pytest.mark.integration
def test_get_of_an_unknown_name_or_revision_is_not_found(serving) -> None:
    home, _ = serving

    for reference in ("missing", "implement", "implement-review@9"):
        result = _graph_list(home, "get", reference)
        assert result.returncode == 1
        assert result.stderr.startswith("oe graph get: NOT_FOUND: "), result.stderr
        assert result.stdout == ""


@pytest.mark.integration
def test_project_selects_the_graph_applied_listed_and_read(serving) -> None:
    home, default_id = serving

    applied = _graph_list(home, "apply", str(EXAMPLE), "--project", "cli-alpha")
    assert applied.returncode == 0, applied.stderr
    alpha_id = applied.stdout.split("(graph ")[1].rstrip(")\n")
    assert applied.stdout == f"created implement-review revision 1 in project cli-alpha (graph {alpha_id})\n"
    assert alpha_id != default_id

    for listed in (_graph_list(home, "list", "--project", "cli-alpha"), _graph_list(home, "list", OE_PROJECT="cli-alpha")):
        assert [row.split()[:4] for row in listed.stdout.splitlines()[1:]] == [["cli-alpha", "implement-review", alpha_id, "1"]]
    # The flag wins over OE_PROJECT.
    assert default_id in _graph_list(home, "list", "--project", "default", OE_PROJECT="cli-alpha").stdout

    got = _graph_list(home, "get", "implement-review", OE_PROJECT="cli-alpha")
    assert got.stdout.splitlines()[0] == f"implement-review revision 1 of 1 in project cli-alpha (graph {alpha_id})"
    missing = _graph_list(home, "get", "implement-review", "--project", "cli-gamma")
    assert missing.returncode == 1
    assert missing.stderr == "oe graph get: NOT_FOUND: no graph named implement-review in project cli-gamma\n"


@pytest.mark.parametrize("raw", [b"not-json", b"[]", b'{"status":"ok"}',
                                  b'{"request_id":"req_wrong","status":"ok","result":{}}'])
def test_malformed_service_reply_is_actionable_nonzero(monkeypatch, capsys, raw) -> None:
    from openengine.cli.commands import graph
    from openengine.messaging import Client

    class BrokenService:
        async def request(self, *args, **kwargs):
            class Reply:
                data = raw
            return Reply()

    async def pages(*args, **kwargs):
        return [await Client(BrokenService()).query("oe.v1.ge.query.graphs.list")]

    monkeypatch.setattr(graph, "_nats_url", lambda: "nats://127.0.0.1:4222")
    monkeypatch.setattr(graph, "_pages", pages)
    result = graph.run_list(argparse.Namespace(project=None, cursor=None, json=True))
    output = capsys.readouterr()
    assert result == 1
    assert output.out == ""
    assert "INTERNAL" in output.err
    assert "invalid service response" in output.err
    assert "oe logs" in output.err
    assert "Traceback" not in output.err
