import json

import pytest

from open_verify.cli import main


def test_change_mode_supplies_default_request_without_prompt(monkeypatch):
    received = []

    async def run(args):
        received.append(args)
        return 0

    monkeypatch.setattr("open_verify.cli.run", run)
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("Must not prompt"))
    assert main(["--base", "main", "--head", "HEAD", "--include-working-tree"]) == 0
    assert received[0].request == "Verify the meaningful behavior affected by this change."
    assert received[0].include_working_tree


@pytest.mark.parametrize("args", [["--head", "HEAD"], ["--include-working-tree"]])
def test_change_options_require_base(args):
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2


@pytest.mark.parametrize('args', [
    ['--publish'], ['--setup-file', '.env'],
    ['--pr', 'https://github.com/o/r/pull/1', '--base', 'main'],
    ['--pr', 'https://github.com/o/r/pull/1', '--publish'],
    ['--pr', 'https://github.com/o/r/pull/1', '--publish', '--allow-exec', '--plan-only'],
])
def test_pr_flag_combinations_are_validated(args):
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2


def test_pr_supplies_default_request_without_prompt(monkeypatch):
    received = []

    async def run(args):
        received.append(args)
        return 0

    monkeypatch.setattr('open_verify.cli.run', run)
    monkeypatch.setattr('builtins.input', lambda *_: pytest.fail('Must not prompt'))
    assert main(['--pr', 'https://github.com/o/r/pull/1', '--allow-exec', '--publish']) == 0
    assert received[0].request and received[0].publish
    assert received[0].max_cases == 1


def test_publication_only_does_not_start_verification(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    publish = AsyncMock(return_value=0)
    monkeypatch.setattr('open_verify.github.publish_saved', publish)
    monkeypatch.setattr('open_verify.cli.run_local', lambda *_: pytest.fail('Must not run tests'))
    monkeypatch.setattr('builtins.input', lambda *_: pytest.fail('Must not prompt'))
    assert main(['--publish-from', str(tmp_path)]) == 0
    publish.assert_awaited_once_with(tmp_path)


@pytest.mark.parametrize('extra', [['test login'], ['--allow-exec'], ['--pr', 'https://github.com/o/r/pull/1'], ['--plan-only']])
def test_publication_only_rejects_execution_options(tmp_path, extra):
    with pytest.raises(SystemExit):
        main(['--publish-from', str(tmp_path), *extra])


def test_change_preflight_failure_saves_blocked_manifest_without_starting_agent(
    tmp_path, monkeypatch
):
    (tmp_path / ".git").mkdir()

    async def read_change(*args, **kwargs):
        raise ValueError("Requested head does not match checkout")

    monkeypatch.setattr("open_verify.cli.read_change", read_change)
    monkeypatch.setattr(
        "open_verify.cli.provider_for", lambda *_: pytest.fail("Must not start agent")
    )
    assert (
        main(["--base", "main", "--project", str(tmp_path), "--output", str(tmp_path / "runs")])
        == 2
    )
    (path,) = (tmp_path / "runs").glob("*/manifest.json")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["status"] == "blocked"
    assert manifest["artifacts"] == []
    assert "head does not match" in manifest["reason"]
