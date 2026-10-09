"""The `engine` command's top level."""

from __future__ import annotations

import pytest

from engine.apps.cli import __main__ as cli


def test_bare_engine_prints_the_top_level_commands(capsys):
    assert cli.main([]) == 0

    usage = capsys.readouterr().out
    for command in ("connect", "connections", "disconnect", "agent", "agents", "daemon", "graph", "graphs", "run", "loop", "loops", "node", "nodes", "backend", "backends"):
        assert command in usage
    assert "runner" not in usage and "SUPPRESS" not in usage
    assert "--json" in usage


def test_the_hidden_runner_command_still_parses():
    assert cli.parser().parse_args(["runner", "signin", "codex"]).command == "runner"


@pytest.mark.parametrize("command", ["backends", "graphs", "runs", "loops", "nodes", "agents", "connections"])
def test_list_commands_print_tables_unless_asked_for_json(command):
    extra = ["--run", "run-1"] if command == "nodes" else []
    assert cli.parser().parse_args([command, *extra]).json is False
    assert cli.parser().parse_args([command, *extra, "--json"]).json is True


@pytest.mark.parametrize("command", ["status", "doctor", "review", "init"])
def test_removed_commands_are_refused(command, capsys):
    with pytest.raises(SystemExit) as exited:
        cli.main([command])

    assert exited.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
