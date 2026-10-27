"""GE started by the supervisor (`oe up`), queried and applied to over real NATS, drained on stop, and refused when it cannot be ready."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from urllib.request import urlopen

import nats
import pytest
import yaml
from nats.errors import TimeoutError as NatsTimeoutError
from openengine.contracts.validate import load_contract, validate_message
from openengine.messaging import Client, ServiceError

pytestmark = pytest.mark.integration

APPLY = "oe.v1.ge.control.graphs.apply"
EXAMPLE = Path(__file__).parents[1] / "packages/contracts/graph_engine/v1/resources/examples/implement-review.graph.yaml"


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _oe(state: Path):
    environment = {**os.environ, "XDG_STATE_HOME": str(state), "OE_LOG_FORMAT": "json"}

    def run(*arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "openengine.cli", *arguments],
            env={**environment, **(env or {})},
            capture_output=True,
            text=True,
            timeout=240,
        )

    return run


@pytest.fixture
def oe(tmp_path: Path):
    run = _oe(tmp_path / "state")
    yield run
    run("down")


def _up(oe, port: int, **env: str) -> subprocess.CompletedProcess[str]:
    return oe("up", "--nats-port", str(port), "--nats-monitor-port", str(_free_port()), env=env)


async def _request(url: str, subject: str, body: dict) -> dict:
    connection = await nats.connect(url)
    try:
        return json.loads((await connection.request(subject, json.dumps(body).encode(), timeout=5)).data)
    finally:
        await connection.close()


def _ge_lines(oe) -> list[dict]:
    lines = [json.loads(line) for line in oe("logs", "--json").stdout.splitlines() if line.startswith("{")]
    return [line for line in lines if line["service.name"] == "openengine-ge"]


def test_ge_serves_graphs_list_once_ready_and_drains_on_stop(oe) -> None:
    port = _free_port()
    up = _up(oe, port)
    assert up.returncode == 0, up.stderr

    state = json.loads(oe("status", "--json").stdout)
    ge = next(process for process in state["processes"] if process["name"] == "ge")
    assert ge["status"] == "ready"
    assert ge["address"].startswith("http://127.0.0.1:") and ge["address"].endswith("/healthz")
    with urlopen(ge["address"], timeout=2) as health:
        assert health.status == 200

    subject = "oe.v1.ge.query.graphs.list"
    reply = asyncio.run(_request(f"nats://127.0.0.1:{port}", subject, {"request_id": "req_it", "params": {}}))
    assert reply == {"request_id": "req_it", "status": "ok", "result": {"items": [], "next_cursor": None}}
    assert validate_message(load_contract("graph_engine"), "ListGraphsResponse", reply, in_reply_to=subject) == []

    bad = asyncio.run(_request(f"nats://127.0.0.1:{port}", subject, {"params": {"unknown": 1}}))
    assert bad["error"]["code"] == "INVALID_ARGUMENT"

    assert oe("down").returncode == 0
    bodies = [line["body"] for line in _ge_lines(oe)]
    served = bodies[bodies.index("nats connected"):]
    assert served[:4] == ["nats connected", "serving oe.v1.ge.query.>", "serving oe.v1.ge.control.>", "ready"]
    # The unknown param passes the envelope check, so it is logged as a request before graphs.list's schema rejects it.
    assert served[4:6] == ["graphs.list request", "graphs.list request"]
    assert served[6].startswith("graphs.list rejected: INVALID_ARGUMENT")
    assert served[7:] == ["draining", "stopped"]


def test_supervisor_fails_when_ge_cannot_become_ready(oe, tmp_path: Path) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        # GE cannot bind its health port, so it never reports ready.
        up = _up(oe, _free_port(), OE_GE_HEALTH_PORT=str(taken.getsockname()[1]))

    assert up.returncode == 1
    assert "ge exited with status 1 before it was ready" in up.stderr
    assert oe("status").returncode == 1
    # The supervisor has exited; its last state stays behind for `oe up` to report.
    state = json.loads((tmp_path / "state" / "openengine" / "supervisor.json").read_text())
    assert state["status"] == "failed"
    assert state["error"] == "ge exited with status 1 before it was ready"
    assert {process["name"]: process["status"] for process in state["processes"]}["ge"] == "failed"


def test_ge_stops_on_sigterm_while_nats_is_unreachable(tmp_path: Path) -> None:
    ge = subprocess.Popen(
        [sys.executable, "-m", "graph_engine"],
        env={**os.environ, "XDG_STATE_HOME": str(tmp_path / "state"),
             "OE_NATS_URL": f"nats://127.0.0.1:{_free_port()}", "OE_GE_READY_FILE": str(tmp_path / "ready")},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            ge.wait(timeout=2)  # still retrying its first connection
        ge.terminate()
        assert ge.wait(timeout=5) == 0
    finally:
        ge.kill()
    assert not (tmp_path / "ready").exists()


@pytest.fixture(scope="module")
def ge_url(tmp_path_factory: pytest.TempPathFactory):
    """One running OpenEngine for the apply tests, each of which applies graphs of its own names."""
    run, port = _oe(tmp_path_factory.mktemp("apply") / "state"), _free_port()
    up = _up(run, port)
    assert up.returncode == 0, up.stderr
    yield f"nats://127.0.0.1:{port}"
    run("down")


def _graph(name: str, description: str | None = None) -> dict:
    graph = yaml.safe_load(EXAMPLE.read_text())
    graph["metadata"]["name"] = name
    graph["spec"]["description"] = description or graph["spec"]["description"]
    return graph


class DropFirstReply:
    """A NATS requester whose first reply is lost after GE has handled the request, as a timeout would lose it."""

    def __init__(self, connection) -> None:
        self.connection = connection
        self.requests = 0

    async def request(self, subject: str, payload: bytes = b"", timeout: float = 0.5, old_style: bool = False, headers=None):
        self.requests += 1
        reply = await self.connection.request(subject, payload, timeout=timeout, headers=headers)
        if self.requests == 1:
            raise NatsTimeoutError
        return reply


async def _session(url: str, scenario):
    connection = await nats.connect(url)
    try:
        return await scenario(connection)
    finally:
        await connection.close()


async def _result(client: Client, reply: dict) -> dict:
    subject = f"oe.v1.ge.query.operations.{reply['operation_id']}.get"
    response = await client.query(subject)
    assert validate_message(load_contract("graph_engine"), "GetOperationResponse", response, in_reply_to=subject) == []
    operation = response["result"]["operation"]
    assert operation["status"] == "succeeded"
    return operation["result"]


def test_apply_retries_a_lost_reply_without_adding_a_revision(ge_url: str) -> None:
    async def scenario(connection) -> None:
        dropping = DropFirstReply(connection)
        reply = await Client(dropping, timeout=5).command(APPLY, {"graph": _graph("lost-reply")}, command_id="cmd_lost_reply")
        assert dropping.requests == 2
        assert validate_message(load_contract("graph_engine"), "ApplyGraphResponse", reply, in_reply_to=APPLY) == []

        client = Client(connection, timeout=5)
        assert await _result(client, reply) == {"graph": {"graph_id": reply["resource_id"], "revision": 1}, "created": True}
        again = await client.command(APPLY, {"graph": _graph("lost-reply")})
        assert await _result(client, again) == {"graph": {"graph_id": reply["resource_id"], "revision": 1}, "created": False}

    asyncio.run(_session(ge_url, scenario))


def test_concurrent_apply_neither_duplicates_nor_overwrites_revisions(ge_url: str) -> None:
    async def scenario(connection) -> None:
        client = Client(connection, timeout=10)
        same = await asyncio.gather(*(client.command(APPLY, {"graph": _graph("concurrent")}) for _ in range(6)))
        retried = await asyncio.gather(
            *(client.command(APPLY, {"graph": _graph("concurrent", "Retried.")}, command_id="cmd_concurrent") for _ in range(6))
        )
        changed = await asyncio.gather(
            *(client.command(APPLY, {"graph": _graph("concurrent", f"Change {n}.")}) for n in range(6))
        )

        graph_ids = {reply["resource_id"] for reply in same + retried + changed}
        assert len(graph_ids) == 1
        assert sorted([(await _result(client, reply))["created"] for reply in same]) == [False] * 5 + [True]
        assert len({reply["operation_id"] for reply in retried}) == 1
        assert (await _result(client, retried[0]))["graph"]["revision"] == 2
        assert sorted([(await _result(client, reply))["graph"]["revision"] for reply in changed]) == list(range(3, 9))

    asyncio.run(_session(ge_url, scenario))


def test_apply_rejects_conflicting_command_reuse(ge_url: str) -> None:
    async def scenario(connection) -> None:
        client = Client(connection, timeout=5)
        first = await client.command(APPLY, {"graph": _graph("reused")}, command_id="cmd_reused")
        with pytest.raises(ServiceError) as conflict:
            await client.command(APPLY, {"graph": _graph("reused", "Changed.")}, command_id="cmd_reused")
        assert conflict.value.code == "COMMAND_ID_CONFLICT"
        assert conflict.value.details == {
            "command_id": "cmd_reused", "original_subject": APPLY, "original_operation_id": first["operation_id"],
        }
        assert await client.command(APPLY, {"graph": _graph("reused")}, command_id="cmd_reused") == first

        with pytest.raises(ServiceError) as python:
            await client.command(APPLY, {"python": {"name": "reused", "source": "workflow = None"}})
        assert python.value.code == "UNSUPPORTED_CAPABILITY"

    asyncio.run(_session(ge_url, scenario))


def test_apply_rejects_an_expected_version_mismatch(ge_url: str) -> None:
    async def scenario(connection) -> None:
        client = Client(connection, timeout=5)
        await client.command(APPLY, {"graph": _graph("versioned")})
        changed = await client.command(APPLY, {"graph": _graph("versioned", "Changed.")}, expected_version=1)
        assert (await _result(client, changed))["graph"]["revision"] == 2

        for _ in range(2):  # the retry gets the same rejection
            with pytest.raises(ServiceError) as stale:
                await client.command(APPLY, {"graph": _graph("versioned", "Stale.")}, command_id="cmd_stale", expected_version=1)
            assert stale.value.code == "VERSION_CONFLICT"
            assert stale.value.details == {"expected_version": 1, "actual_version": 2}

    asyncio.run(_session(ge_url, scenario))


def test_apply_from_the_cli_survives_a_ge_restart(oe, tmp_path: Path) -> None:
    changed = tmp_path / "changed.graph.yaml"
    changed.write_text(EXAMPLE.read_text().replace(_graph("x")["spec"]["description"], "Changed."))
    port = _free_port()
    assert _up(oe, port).returncode == 0

    created = oe("graph", "apply", str(EXAMPLE))
    graph_id = created.stdout.split("(graph ")[1].rstrip(")\n")
    assert created.stdout == f"created implement-review revision 1 (graph {graph_id})\n", created.stderr
    assert oe("graph", "apply", str(EXAMPLE)).stdout == f"unchanged implement-review revision 1 (graph {graph_id})\n"
    assert oe("graph", "apply", str(changed)).stdout == f"updated implement-review revision 2 (graph {graph_id})\n"
    retried = asyncio.run(_session(f"nats://127.0.0.1:{port}", lambda connection: Client(connection).command(
        APPLY, {"graph": _graph("restarted")}, command_id="cmd_before_restart")))

    assert oe("down").returncode == 0
    port = _free_port()
    assert _up(oe, port).returncode == 0

    applied = json.loads(oe("graph", "apply", str(changed), "--json").stdout)
    assert applied["response"]["resource_id"] == graph_id
    assert applied["operation"]["result"] == {"graph": {"graph_id": graph_id, "revision": 2}, "created": False}
    assert asyncio.run(_session(f"nats://127.0.0.1:{port}", lambda connection: Client(connection).command(
        APPLY, {"graph": _graph("restarted")}, command_id="cmd_before_restart"))) == retried
