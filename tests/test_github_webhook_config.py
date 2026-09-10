"""The webhook names its repository in TOML and keeps its secret out of it."""

from pathlib import Path

import pytest

import engine.apps.web.__main__ as web_main
from engine.apps.web.github_webhook import (
    SECRET_VARIABLE,
    GitHubWebhookConfig,
    github_webhook_config,
)
from engine.runtime import load_engine_config


@pytest.fixture(autouse=True)
def _no_inherited_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SECRET_VARIABLE, raising=False)


def _loaded(tmp_path: Path, document: str):
    path = tmp_path / "engine.toml"
    path.write_text(document)
    return load_engine_config(path, environ={}, cwd=tmp_path)


def test_secret_is_read_from_the_env_file_beside_the_configuration(
    tmp_path: Path,
) -> None:
    (tmp_path / ".env").write_text(f"{SECRET_VARIABLE}=from-file\n")

    config = github_webhook_config(_loaded(tmp_path, '[github]\nrepository = "o/n"\n'))

    assert config is not None
    assert config.repository == "o/n"
    assert config.current_secret() == "from-file"


def test_rotating_the_secret_file_takes_effect_without_a_restart(
    tmp_path: Path,
) -> None:
    secret_file = tmp_path / ".env"
    secret_file.write_text(f"{SECRET_VARIABLE}=initial\n")
    config = github_webhook_config(_loaded(tmp_path, '[github]\nrepository = "o/n"\n'))
    assert config is not None

    secret_file.write_text(f"{SECRET_VARIABLE}=rotated\n")

    assert config.current_secret() == "rotated"


def test_process_environment_wins_over_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(f"{SECRET_VARIABLE}=from-file\n")
    monkeypatch.setenv(SECRET_VARIABLE, "from-environment")

    config = github_webhook_config(_loaded(tmp_path, '[github]\nrepository = "o/n"\n'))

    assert config is not None
    assert config.current_secret() == "from-environment"


def test_a_secret_without_a_repository_is_still_configured(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"{SECRET_VARIABLE}=from-file\n")

    config = github_webhook_config(_loaded(tmp_path, "default_branch = 'main'\n"))

    assert config is not None
    assert config.repository == ""


def test_naming_neither_leaves_the_webhook_unconfigured(tmp_path: Path) -> None:
    assert github_webhook_config(_loaded(tmp_path, "default_branch = 'main'\n")) is None


def test_a_missing_secret_reads_as_empty_rather_than_failing(tmp_path: Path) -> None:
    config = github_webhook_config(_loaded(tmp_path, '[github]\nrepository = "o/n"\n'))

    assert config is not None
    assert config.current_secret() == ""


def test_the_secret_is_kept_out_of_the_representation(tmp_path: Path) -> None:
    secret_file = tmp_path / ".env"
    secret_file.write_text(f"{SECRET_VARIABLE}=from-file\n")

    assert "from-file" not in repr(GitHubWebhookConfig("o/n", secret_file))


def test_web_entrypoint_reports_the_configured_webhook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "engine.toml"
    path.write_text('[github]\nrepository = "owner/name"\n')
    (tmp_path / ".env").write_text(f"{SECRET_VARIABLE}=from-file\n")
    seen = []
    monkeypatch.setattr(web_main, "report_wiring", seen.append)

    assert web_main.main(["--check", "--config", str(path)]) == 0

    webhook = seen[0].github_webhook
    assert webhook is not None
    assert webhook.repository == "owner/name"
    assert webhook.current_secret() == "from-file"
