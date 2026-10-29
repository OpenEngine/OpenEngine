"""Managed CLI requests cannot downgrade when launch trust is absent or stale."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from openengine.cli.commands import migrate
from openengine.supervisor.broker_launch import Launch


def test_managed_migration_requires_trust_before_database_changes(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(migrate, "nats_url", lambda: "nats://127.0.0.1:4222")
    monkeypatch.setattr(migrate, "state_directory", lambda: tmp_path)

    def unexpected(*args):
        pytest.fail("database changes must not precede managed trust validation")

    monkeypatch.setattr(migrate, "migrate_databases", unexpected)
    assert migrate.run(argparse.Namespace(nats_url=None, streams=None)) == 1
    assert "broker.json" in capsys.readouterr().err


def test_managed_migration_rejects_status_endpoint_from_another_launch(tmp_path: Path, monkeypatch, capsys) -> None:
    with Launch(tmp_path.resolve() / "state", "127.0.0.1", 4222):
        monkeypatch.setattr(migrate, "nats_url", lambda: "tls://127.0.0.1:4223")
        monkeypatch.setattr(migrate, "state_directory", lambda: tmp_path.resolve() / "state")

        def unexpected(*args):
            pytest.fail("database changes must not precede endpoint validation")

        monkeypatch.setattr(migrate, "migrate_databases", unexpected)
        assert migrate.run(argparse.Namespace(nats_url=None, streams=None)) == 1
    assert "endpoint does not match launch trust" in capsys.readouterr().err


def test_explicit_migration_endpoint_does_not_inherit_managed_trust(monkeypatch) -> None:
    endpoint = "nats://127.0.0.1:4333"
    monkeypatch.setattr(migrate, "migrate_databases", lambda config: [])
    monkeypatch.setattr(migrate, "load_stream_configs", lambda directory: [])

    def unexpected(*args):
        pytest.fail("explicit external mode must not load managed trust")

    monkeypatch.setattr(migrate, "load_trust", unexpected)
    observed = []

    async def streams(url, configs, *, trust):
        observed.append((url, trust))
        return []

    monkeypatch.setattr(migrate, "migrate_streams", streams)
    assert migrate.run(argparse.Namespace(nats_url=endpoint, streams=None)) == 0
    assert observed == [(endpoint, None)]


@pytest.mark.parametrize("unsafe", ["state-mode", "log-symlink", "log-mode"])
def test_up_rejects_unsafe_control_storage_before_spawning(tmp_path: Path, monkeypatch, unsafe: str) -> None:
    from openengine.cli.commands import up

    state = tmp_path.resolve() / "state"
    state.mkdir(mode=0o700)
    logs = state / "logs"
    logs.mkdir(mode=0o700)
    log = logs / "supervisor.log"
    if unsafe == "state-mode":
        state.chmod(0o755)
    elif unsafe == "log-symlink":
        log.symlink_to(tmp_path / "target")
    else:
        log.write_text("")
        log.chmod(0o644)
    monkeypatch.setattr(up, "state_directory", lambda: state)
    monkeypatch.setattr(up, "supervisor_log", lambda: log)
    monkeypatch.setattr(up, "running_state", lambda: None)

    def unexpected(*args, **kwargs):
        pytest.fail("unsafe storage must fail before starting the supervisor")

    monkeypatch.setattr(up.subprocess, "Popen", unexpected)
    with pytest.raises(OSError):
        up.run(argparse.Namespace())
