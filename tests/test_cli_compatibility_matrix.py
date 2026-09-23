"""Exercise release discovery without requiring npm or network access."""

import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

spec = importlib.util.spec_from_file_location(
    "cli_compatibility_matrix",
    Path(__file__).resolve().parents[1] / "scripts" / "cli_compatibility_matrix.py",
)
matrix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(matrix)


def test_matrix_selects_three_newest_stable_versions_per_provider(monkeypatch, capsys):
    def npm_versions(command, **kwargs):
        assert command[:2] == ["npm", "view"]
        assert command[3:] == ["versions", "--json"]
        versions = {
            "@openai/codex": ["0.9.0", "0.10.0", "1.0.0-alpha.1", "0.11.0", "0.8.0", "0.11.0"],
            "@anthropic-ai/claude-code": ["2.1.9", "2.1.11", "3.0.0-beta", "2.1.10", "2.1.8"],
        }
        return json.dumps(versions[command[2]])

    monkeypatch.setattr(matrix.subprocess, "check_output", npm_versions)
    matrix.main()
    assert json.loads(capsys.readouterr().out) == {"include": [
        {"provider": provider, "package": package, "version": version}
        for provider, package, versions in [
            ("codex", "@openai/codex", ["0.11.0", "0.10.0", "0.9.0"]),
            ("claude", "@anthropic-ai/claude-code", ["2.1.11", "2.1.10", "2.1.9"]),
        ]
        for version in versions
    ]}


@pytest.mark.parametrize("response", ["[]", '["1.0.0", "2.0.0", "3.0.0-rc.1"]', '{}', 'invalid'])
def test_invalid_or_incomplete_registry_data_emits_no_matrix(response, monkeypatch, capsys):
    monkeypatch.setattr(matrix.subprocess, "check_output", lambda *a, **kw: response)
    with pytest.raises(ValueError):
        matrix.main()
    assert capsys.readouterr().out == ""


def test_registry_failure_emits_no_partial_matrix(monkeypatch, capsys):
    def npm_versions(command, **kwargs):
        if command[2] == "@anthropic-ai/claude-code":
            raise subprocess.CalledProcessError(1, command)
        return '["1.0.0", "2.0.0", "3.0.0"]'

    monkeypatch.setattr(matrix.subprocess, "check_output", npm_versions)
    with pytest.raises(subprocess.CalledProcessError):
        matrix.main()
    assert capsys.readouterr().out == ""
