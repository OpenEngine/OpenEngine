"""The `engine` command's top level."""

from __future__ import annotations

import re

import pytest

from engine.apps.cli import __main__ as cli


def test_bare_engine_prints_the_top_level_commands(capsys):
    assert cli.main([]) == 0

    usage = capsys.readouterr().out
    for command in ("connect", "connection", "disconnect", "agent", "daemon", "graph", "run", "loop", "node", "backend"):
        assert re.search(rf"^    {command}\s", usage, re.MULTILINE)
    for command in cli.graph_commands.HIDDEN:
        assert not re.search(rf"^    {command}\s", usage, re.MULTILINE)
        assert command not in usage.split("positional arguments:")[0].split("Start a change")[0]
    assert "SUPPRESS" not in usage
    assert "list, add, remove and sign in agents" in usage
    assert "--json" in usage


def test_the_hidden_runner_command_still_parses():
    assert cli.parser().parse_args(["runner", "signin", "codex"]).command == "runner"


@pytest.mark.parametrize("command", ["backends", "graphs", "runs", "loops", "nodes", "agents", "connections"])
def test_list_commands_print_tables_unless_asked_for_json(command):
    extra = ["--run", "run-1"] if command == "nodes" else []
    assert cli.parser().parse_args([command, *extra]).json is False
    assert cli.parser().parse_args([command, *extra, "--json"]).json is True


@pytest.mark.parametrize("command", ["status", "review", "init"])
def test_removed_commands_are_refused(command, capsys):
    with pytest.raises(SystemExit) as exited:
        cli.main([command])

    assert exited.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


@pytest.mark.parametrize("option", ["--help", "-h", "--all"])
def test_explicit_help_includes_hidden_commands(option, capsys):
    with pytest.raises(SystemExit) as exited:
        cli.main([option])
    assert exited.value.code == 0
    usage = capsys.readouterr().out
    for command in cli.graph_commands.HIDDEN:
        assert re.search(rf"^    {command}\s", usage, re.MULTILINE)


@pytest.mark.parametrize("singular, extra", [
    ("backend", ["--check", "--json"]),
    ("graph", ["--all-projects", "--json"]),
    ("run", ["--status", "failed", "--limit", "5", "--json"]),
    ("loop", ["--project", "example", "--json"]),
    ("node", ["--run", "run-1", "--json"]),
    ("agent", ["--backend", "local", "--json"]),
    ("verb", ["--project", "example"]),
    ("connection", ["--origin", "https://gitlab.example.com", "--json"]),
])
def test_plural_aliases_parse_and_dispatch_like_singular_list(singular, extra, monkeypatch):
    parser = cli.parser()
    expected = parser.parse_args([singular, "list", *extra])
    assert expected.command == singular
    assert expected.action == "list"
    seen = []
    def handler(arguments):
        seen.append(arguments)
        return 0
    if singular == "connection":
        monkeypatch.setattr(cli.connect, "connections", handler)
    else:
        monkeypatch.setitem(cli.graph_commands._HANDLERS, (singular, "list"), handler)
    for spelling in ([singular, "list"], [f"{singular}s"], [f"{singular}s", "list"]):
        assert parser.parse_args([*spelling, *extra]) == expected
        assert cli.main([*spelling, *extra]) == 0
    assert seen == [expected] * 3


def test_go_help_includes_instruction_and_fix_examples(capsys):
    with pytest.raises(SystemExit) as exited:
        cli.main(["go", "--help"])
    assert exited.value.code == 0
    usage = capsys.readouterr().out
    assert 'engine go "add a widget"' in usage
    assert "engine go fix https://github.com/OpenEngine/OpenEngine/pull/855" in usage
