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

    config = github_webhook_config(_loaded(tmp_path, '[github]\nrepository = "o/n"\n[repos]\n"o/n" = "."\n'))

    assert config is not None
    assert config.repository == "o/n"
    assert config.current_secret() == "from-file"


def test_rotating_the_secret_file_takes_effect_without_a_restart(
    tmp_path: Path,
) -> None:
    secret_file = tmp_path / ".env"
    secret_file.write_text(f"{SECRET_VARIABLE}=initial\n")
    config = github_webhook_config(_loaded(tmp_path, '[github]\nrepository = "o/n"\n[repos]\n"o/n" = "."\n'))
    assert config is not None

    secret_file.write_text(f"{SECRET_VARIABLE}=rotated\n")

    assert config.current_secret() == "rotated"


def test_process_environment_wins_over_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(f"{SECRET_VARIABLE}=from-file\n")
    monkeypatch.setenv(SECRET_VARIABLE, "from-environment")

    config = github_webhook_config(_loaded(tmp_path, '[github]\nrepository = "o/n"\n[repos]\n"o/n" = "."\n'))

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
    config = github_webhook_config(_loaded(tmp_path, '[github]\nrepository = "o/n"\n[repos]\n"o/n" = "."\n'))

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
    path.write_text('[github]\nrepository = "owner/name"\n[repos]\n"owner/name" = "."\n')
    (tmp_path / ".env").write_text(f"{SECRET_VARIABLE}=from-file\n")
    seen = []
    monkeypatch.setattr(web_main, "report_wiring", seen.append)

    assert web_main.main(["--check", "--config", str(path)]) == 0

    webhook = seen[0].github_webhook
    assert webhook is not None
    assert webhook.repository == "owner/name"
    assert webhook.current_secret() == "from-file"


def test_composition_passes_the_target_repository_to_the_http_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import Mock

    loaded = _loaded(tmp_path, '[github]\nrepository = "owner/name"\n[repos]\n"owner/name" = "."\n')
    for name in (
        "build_capabilities", "build_runners", "build_read_only_runners",
        "build_session", "build_graph_runtime",
    ):
        monkeypatch.setattr(web_main, name, Mock())
    create_app = Mock()
    monkeypatch.setattr(web_main, "create_app", create_app)

    web_main.compose_app(loaded, None)

    assert create_app.call_args.kwargs["github_repository"] == "owner/name"


def test_composition_passes_all_webhook_repositories(tmp_path, monkeypatch):
    from unittest.mock import Mock

    loaded = _loaded(tmp_path, '''
[github]
repositories = ["acme/api", "other/web"]
[repos]
"acme/api" = "/api"
"other/web" = "/web"
''')
    for name in (
        "build_capabilities", "build_runners", "build_read_only_runners",
        "build_session", "build_graph_runtime",
    ):
        monkeypatch.setattr(web_main, name, Mock())
    create_app = Mock()
    monkeypatch.setattr(web_main, "create_app", create_app)
    web_main.compose_app(loaded, None)
    assert create_app.call_args.kwargs["github_repositories"] == ("acme/api", "other/web")


@pytest.mark.parametrize("access, expected", [
    (False, "missing write access"),
    (RuntimeError("private credential detail"), "access check failed"),
])
def test_check_reports_access_for_each_repository(tmp_path, monkeypatch, capsys, access, expected):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock, call
    from engine.runtime import Capabilities

    loaded = _loaded(tmp_path, '''
[github]
repositories = ["acme/api", "other/web"]
[repos]
"acme/api" = "/api"
"other/web" = "/web"
''')
    source = SimpleNamespace(
        authenticated_login=AsyncMock(return_value="engine"),
        can_write_repository=AsyncMock(side_effect=[True, access]),
    )
    capabilities = Capabilities(**{
        name: source if name == "source_control" else object()
        for name in Capabilities.__dataclass_fields__
    })
    monkeypatch.setattr(web_main, "build_capabilities", lambda _: capabilities)
    monkeypatch.setattr(web_main, "build_runners", lambda _: {})
    monkeypatch.setattr(web_main, "build_read_only_runners", lambda _: {})
    monkeypatch.setattr(web_main, "build_session", Mock(return_value=SimpleNamespace(profiles={})))
    monkeypatch.setattr(web_main, "gh_cli_status", lambda: SimpleNamespace(authenticated=True, account="engine"))
    web_main.report_wiring(web_main._settings(loaded))
    output = capsys.readouterr().out
    assert "github acme/api: write access" in output
    assert f"github other/web: {expected}" in output
    assert "private credential detail" not in output
    assert source.can_write_repository.await_args_list == [
        call("https://github.com/acme/api/pull/1", "engine"),
        call("https://github.com/other/web/pull/1", "engine"),
    ]
