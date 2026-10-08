"""The `engine` command's top level."""

from __future__ import annotations

import pytest

from engine.apps.cli import __main__ as cli


def test_bare_engine_prints_the_top_level_commands(capsys):
    assert cli.main([]) == 0

    usage = capsys.readouterr().out
    for command in ("connect", "agent", "daemon", "graph", "graphs", "run", "loop", "loops", "node", "nodes", "runner", "backend", "backends"):
        assert command in usage


@pytest.mark.parametrize("command", ["status", "doctor", "review", "init"])
def test_removed_commands_are_refused(command, capsys):
    with pytest.raises(SystemExit) as exited:
        cli.main([command])

    assert exited.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
