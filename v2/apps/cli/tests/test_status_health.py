"""Current status is an observation and never overwrites startup state."""

import argparse
import json
import os
from urllib.error import URLError

import pytest

from openengine.cli.commands import status
from openengine.supervisor.state import ProcessState, Status, SupervisorState


@pytest.mark.parametrize("healthy", [True, False])
def test_status_probes_health_with_a_bound_and_retains_address(monkeypatch, capsys, healthy):
    url = "http://127.0.0.1:1234/healthz"
    recorded = SupervisorState(os.getpid(), Status.READY, [ProcessState("ge", os.getpid(), url, Status.READY)])
    monkeypatch.setattr(status, "running_state", lambda: recorded)
    probes = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def probe(address, *, timeout):
        probes.append((address, timeout))
        if not healthy:
            raise URLError("timeout")
        return Response()

    monkeypatch.setattr(status, "urlopen", probe)
    assert status.run(argparse.Namespace(json=True)) == (0 if healthy else 1)
    observed = json.loads(capsys.readouterr().out)
    expected = "ready" if healthy else "unready"
    assert observed["status"] == expected
    assert observed["processes"][0]["status"] == expected
    assert observed["processes"][0]["address"] == url
    assert probes == [(url, 0.5)]
    assert recorded.status == Status.READY
    assert recorded.processes[0].status == Status.READY


@pytest.mark.parametrize("state", [Status.STARTING, Status.FAILED])
def test_existing_but_not_ready_supervisor_is_nonzero(monkeypatch, capsys, state):
    monkeypatch.setattr(status, "running_state", lambda: SupervisorState(os.getpid(), state))
    assert status.run(argparse.Namespace(json=True)) == 1
    assert json.loads(capsys.readouterr().out)["status"] == state
