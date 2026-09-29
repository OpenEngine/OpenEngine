"""`engine init` onboards the checkout it runs in."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

from engine.apps.cli import __main__ as cli
from engine.apps.cli import onboarding


def _checkout(path: Path, origin: str | None = None) -> Path:
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    if origin:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
    return path.resolve()


def _init(monkeypatch, directory: Path, config: Path, *arguments: str) -> int:
    monkeypatch.chdir(directory)
    return cli.main(["init", "--config", str(config), "--no-restart", *arguments])


def test_init_adds_the_checkout_to_the_existing_repos_table(monkeypatch, tmp_path, capsys):
    checkout = _checkout(tmp_path / "api", "git@github.com:Acme/api.git")
    (checkout / "src").mkdir()
    config = tmp_path / "engine.toml"
    config.write_text(
        '# Deployment.\n[repos]\n"other" = "~/other"\n\n# Next.\n[server]\nport = 4364\n'
    )

    assert _init(monkeypatch, checkout / "src", config) == 0

    text = config.read_text()
    assert tomllib.loads(text)["repos"] == {"other": "~/other", "acme/api": str(checkout)}
    assert text.startswith("# Deployment.\n") and "\n\n# Next.\n[server]" in text
    assert "Onboarded" in capsys.readouterr().out


def test_init_creates_the_table_and_names_a_remoteless_checkout_by_directory(monkeypatch, tmp_path):
    checkout = _checkout(tmp_path / "scratch")
    config = tmp_path / "engine.toml"
    config.write_text('[server]\nport = 4364\n')

    assert _init(monkeypatch, checkout, config) == 0

    document = tomllib.loads(config.read_text())
    assert document["repos"] == {"scratch": str(checkout)}
    assert document["server"] == {"port": 4364}


def test_init_is_idempotent(monkeypatch, tmp_path, capsys):
    checkout = _checkout(tmp_path / "api", "https://github.com/acme/api.git")
    config = tmp_path / "engine.toml"
    config.write_text("")
    assert _init(monkeypatch, checkout, config) == 0
    before = config.read_text()

    assert _init(monkeypatch, checkout, config) == 0

    assert config.read_text() == before
    assert "already onboarded as 'acme/api'" in capsys.readouterr().out


def test_init_refuses_a_name_already_taken(monkeypatch, tmp_path, capsys):
    checkout = _checkout(tmp_path / "api")
    config = tmp_path / "engine.toml"
    config.write_text('[repos]\n"api" = "/elsewhere"\n')

    assert _init(monkeypatch, checkout, config) == 1
    assert "choose another with --name" in capsys.readouterr().err
    assert _init(monkeypatch, checkout, config, "--name", "api-2") == 0
    assert tomllib.loads(config.read_text())["repos"]["api-2"] == str(checkout)


def test_init_outside_a_repository_fails(monkeypatch, tmp_path, capsys):
    config = tmp_path / "engine.toml"
    config.write_text("")
    (tmp_path / "plain").mkdir()

    assert _init(monkeypatch, tmp_path / "plain", config) == 1
    assert "not inside a git repository" in capsys.readouterr().err


def test_restart_leaves_a_stopped_service_stopped(monkeypatch):
    class Spec:
        url = "http://127.0.0.1:4364"

    monkeypatch.setattr(onboarding.daemon, "current", lambda: (None, Spec()))
    monkeypatch.setattr(onboarding.daemon, "health", lambda url: ("down", None))
    monkeypatch.setattr(onboarding.daemon, "stop_service", lambda: (_ for _ in ()).throw(AssertionError))

    assert "next starts" in onboarding.restart_service()


def test_restart_restarts_a_running_service(monkeypatch):
    class Spec:
        url = "http://127.0.0.1:4364"

    calls = []
    monkeypatch.setattr(onboarding.daemon, "current", lambda: (None, Spec()))
    monkeypatch.setattr(onboarding.daemon, "health", lambda url: ("ready", {}))
    monkeypatch.setattr(onboarding.daemon, "stop_service", lambda: calls.append("stop"))
    monkeypatch.setattr(onboarding.daemon, "start_service", lambda: calls.append("start"))

    assert "Restarted" in onboarding.restart_service()
    assert calls == ["stop", "start"]


def test_init_keeps_the_config_owner_only(monkeypatch, tmp_path):
    checkout = _checkout(tmp_path / "api")
    config = tmp_path / "engine.toml"
    config.write_text("")
    config.chmod(0o600)

    assert _init(monkeypatch, checkout, config) == 0

    assert config.stat().st_mode & 0o777 == 0o600


def test_init_names_the_remote_without_its_credentials(monkeypatch, tmp_path):
    checkout = _checkout(tmp_path / "api", "https://x-access-token:secret@github.com/acme/api.git")
    config = tmp_path / "engine.toml"
    config.write_text("")

    assert _init(monkeypatch, checkout, config) == 0

    assert tomllib.loads(config.read_text())["repos"] == {"acme/api": str(checkout)}


def test_init_warns_that_github_sign_in_widens(monkeypatch, tmp_path, capsys):
    for key in ("CLIENT_ID", "REDIRECT_URI", "CLIENT_SECRET"):
        monkeypatch.delenv(f"ENGINE_GITHUB_LOGIN_{key}", raising=False)
    checkout = _checkout(tmp_path / "api", "git@github.com:acme/api.git")
    config = tmp_path / "engine.toml"
    config.write_text("")
    (tmp_path / ".env").write_text("ENGINE_GITHUB_LOGIN_CLIENT_SECRET=s3cret\n")

    assert _init(monkeypatch, checkout, config) == 0

    assert "anyone with write access to acme/api can now sign in" in capsys.readouterr().err


def test_init_does_not_warn_without_github_sign_in(monkeypatch, tmp_path, capsys):
    for key in ("CLIENT_ID", "REDIRECT_URI", "CLIENT_SECRET"):
        monkeypatch.delenv(f"ENGINE_GITHUB_LOGIN_{key}", raising=False)
    checkout = _checkout(tmp_path / "api", "git@github.com:acme/api.git")
    config = tmp_path / "engine.toml"
    config.write_text("")

    assert _init(monkeypatch, checkout, config) == 0

    assert "sign in" not in capsys.readouterr().err
