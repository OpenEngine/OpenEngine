"""What has to be true for OpenEngine to run outside a checkout.

Three separate claims, and each is a different way the same thing goes wrong --
the program silently depending on the directory it was launched from:

* the client is package data, so the server finds it relative to itself;
* configuration and the conversation database resolve to the user's
  directories, so the same command run from two places is the same install;
* `openengine` is the command, and it can say which build it is.

The archive that carries all three is smoke-tested on a clean machine by
`.github/workflows/release-archive.yml`; these pin the parts that a test can
hold still.
"""

import json
import re
import tomllib
from pathlib import Path

import pytest

import engine.apps.web.build as build_module
import engine.apps.web.cli as web_cli
from engine.apps.web.build import BuildInfo, build_info
from engine.apps.web.composition import Settings
from engine.runtime import load_engine_config
from engine.runtime.paths import (
    DATABASE_NAME,
    default_database_path,
    ensure_parent_directory,
    user_config_directory,
    user_data_directory,
)

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
WEB_APP = REPOSITORY_ROOT / "apps" / "web"

#: Where the client has to land for it to be package data, relative to the
#: distribution root that both Vite and hatchling are configured against.
PACKAGED_CLIENT = "src/engine/apps/web/client"


# --- state lives with the user, not with the launch directory ---------------


def test_macos_keeps_state_in_application_support(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}

    data = user_data_directory(environ=environ, platform="darwin")
    config = user_config_directory(environ=environ, platform="darwin")

    assert data == tmp_path / "Library" / "Application Support" / "OpenEngine"
    # macOS does not separate the two, so neither do we.
    assert config == data


def test_linux_follows_the_xdg_base_directories(tmp_path: Path) -> None:
    environ = {
        "HOME": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    }

    assert user_data_directory(environ=environ, platform="linux") == (
        tmp_path / "data" / "openengine"
    )
    assert user_config_directory(environ=environ, platform="linux") == (
        tmp_path / "config" / "openengine"
    )


def test_linux_defaults_when_no_xdg_base_is_set(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path)}

    assert user_data_directory(environ=environ, platform="linux") == (
        tmp_path / ".local" / "share" / "openengine"
    )
    assert user_config_directory(environ=environ, platform="linux") == (
        tmp_path / ".config" / "openengine"
    )


def test_a_relative_xdg_base_is_ignored(tmp_path: Path) -> None:
    """The specification says so, and it is the whole point besides.

    A relative base would be resolved against the current directory, which is
    the one thing an installed command must not consult.
    """
    environ = {"HOME": str(tmp_path), "XDG_DATA_HOME": "relative/share"}

    assert user_data_directory(environ=environ, platform="linux") == (
        tmp_path / ".local" / "share" / "openengine"
    )


def test_the_data_directory_can_be_relocated_whole(tmp_path: Path) -> None:
    environ = {"HOME": str(tmp_path), "ENGINE_DATA_DIR": str(tmp_path / "elsewhere")}

    assert user_data_directory(environ=environ, platform="linux") == (
        tmp_path / "elsewhere"
    )
    assert default_database_path(environ=environ, platform="linux") == (
        tmp_path / "elsewhere" / DATABASE_NAME
    )


def test_the_default_database_is_not_in_the_launch_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ENGINE_DATA_DIR", str(tmp_path / "state"))

    database = Path(Settings().sqlite_path)

    assert database == tmp_path / "state" / DATABASE_NAME
    # Composing has to be able to open it, and nothing has made `state` yet.
    ensure_parent_directory(database)
    assert database.parent.is_dir()


def test_settings_read_the_environment_that_is_live_when_they_compose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A module-level constant would have frozen the first answer forever."""
    monkeypatch.setenv("ENGINE_DATA_DIR", str(tmp_path / "first"))
    first = Settings().sqlite_path
    monkeypatch.setenv("ENGINE_DATA_DIR", str(tmp_path / "second"))
    second = Settings().sqlite_path

    assert Path(first).parent != Path(second).parent


# --- configuration gains a user-level source, and loses nothing -------------


def test_configuration_falls_back_to_the_user_directory(tmp_path: Path) -> None:
    home, launched_from = tmp_path / "home", tmp_path / "somewhere"
    launched_from.mkdir()
    user_config = home / ".config" / "openengine"
    user_config.mkdir(parents=True)
    (user_config / "engine.toml").write_text('[approvals]\nallow = ["mcp"]\n')

    loaded = load_engine_config(
        environ={"HOME": str(home)}, cwd=launched_from, platform="linux"
    )

    assert loaded.path == (user_config / "engine.toml").resolve()


def test_a_file_beside_the_checkout_still_wins(tmp_path: Path) -> None:
    """The new source is last, so a checkout keeps behaving like a checkout."""
    home, launched_from = tmp_path / "home", tmp_path / "somewhere"
    launched_from.mkdir()
    user_config = home / ".config" / "openengine"
    user_config.mkdir(parents=True)
    (user_config / "engine.toml").write_text('[approvals]\nallow = ["mcp"]\n')
    local = launched_from / "engine.toml"
    local.write_text('[approvals]\nallow = ["edit"]\n')

    loaded = load_engine_config(
        environ={"HOME": str(home)}, cwd=launched_from, platform="linux"
    )

    assert loaded.path == local.resolve()


def test_no_configuration_anywhere_is_still_defaults(tmp_path: Path) -> None:
    loaded = load_engine_config(
        environ={"HOME": str(tmp_path)}, cwd=tmp_path, platform="linux"
    )

    assert loaded.path is None


# --- the client ships inside the Python package -----------------------------


def test_the_server_looks_for_its_client_inside_the_package() -> None:
    package = Path(web_cli.__file__).resolve().parent

    assert web_cli.STATIC_DIRECTORY.parent == package


def test_vite_and_hatchling_agree_on_where_the_client_goes() -> None:
    """The one place these two build systems have to say the same thing.

    Vite decides where the client is written; hatchling decides what the wheel
    carries. They are configured in different files in different languages, and
    a wheel missing its client is a 503 that only shows up after release.
    """
    vite_config = (WEB_APP / "vite.config.ts").read_text()
    out_dir = re.search(r'outDir:\s*"([^"]+)"', vite_config)
    assert out_dir is not None, "vite.config.ts does not set an explicit outDir"
    assert out_dir.group(1) == PACKAGED_CLIENT
    assert (WEB_APP / out_dir.group(1)) == web_cli.STATIC_DIRECTORY

    pyproject = tomllib.loads((WEB_APP / "pyproject.toml").read_text())
    artifacts = pyproject["tool"]["hatch"]["build"]["artifacts"]
    assert f"{PACKAGED_CLIENT}/**" in artifacts, (
        "the client is git-ignored, so hatchling leaves it out of the wheel "
        "unless it is listed as a build artifact"
    )


def test_openengine_is_an_installed_console_script() -> None:
    pyproject = tomllib.loads((WEB_APP / "pyproject.toml").read_text())
    scripts = pyproject["project"]["scripts"]

    assert scripts["openengine"] == "engine.apps.web.cli:main"
    # The operator-facing names are a compatibility promise, not a leftover.
    assert scripts["engine-web"] == "engine.apps.web.__main__:main"


# --- the command can say which build it is ----------------------------------


def test_a_development_install_reports_no_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(build_module, "BUILD_FILE", tmp_path / "absent.json")

    info = build_info()

    assert info.commit == "unknown"
    assert info.version  # whatever is installed, but never empty


def test_a_release_build_reports_the_commit_it_was_built_from(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stamp = tmp_path / "build.json"
    stamp.write_text(json.dumps({"version": "1.2.3", "commit": "abc123"}))
    monkeypatch.setattr(build_module, "BUILD_FILE", stamp)

    assert build_info() == BuildInfo(version="1.2.3", commit="abc123")
    assert str(build_info()) == "openengine 1.2.3 (abc123)"


def test_a_corrupt_build_stamp_does_not_stop_version_answering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stamp = tmp_path / "build.json"
    stamp.write_text("{not json")
    monkeypatch.setattr(build_module, "BUILD_FILE", stamp)

    assert build_info().commit == "unknown"


# --- `openengine` is the command --------------------------------------------


def _record_serve(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def serve(
        config: str | None = None,
        check: bool = False,
        host: str | None = None,
        port: int | None = None,
    ) -> int:
        calls.append({"config": config, "check": check, "host": host, "port": port})
        return 0

    monkeypatch.setattr(web_cli, "serve", serve)
    return calls


def _served(**overrides: object) -> dict[str, object]:
    return {"config": None, "check": False, "host": None, "port": None, **overrides}


def test_bare_openengine_starts_the_web_interface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_serve(monkeypatch)

    assert web_cli.main([]) == 0

    assert calls == [_served()]


def test_openengine_web_is_the_same_command_spelled_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _record_serve(monkeypatch)
    config = str(tmp_path / "engine.toml")

    assert web_cli.main(["web", "--config", config, "--check"]) == 0

    assert calls == [_served(config=config, check=True)]


def test_options_may_be_given_without_naming_the_subcommand(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _record_serve(monkeypatch)
    config = str(tmp_path / "engine.toml")

    assert web_cli.main(["--config", config]) == 0

    assert calls == [_served(config=config)]


def test_the_binding_can_be_moved_off_the_default_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What lets a smoke test run on a machine that already uses 8000."""
    calls = _record_serve(monkeypatch)

    assert web_cli.main(["--host", "127.0.0.1", "--port", "8931"]) == 0

    assert calls == [_served(host="127.0.0.1", port=8931)]


def test_the_default_binding_is_loopback_on_8000(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Left alone, an install serves the interface to this machine only."""
    seen: list[Settings] = []
    monkeypatch.setattr(web_cli, "report_wiring", seen.append)
    monkeypatch.setenv("ENGINE_DATA_DIR", str(tmp_path))

    assert web_cli.main(["--check"]) == 0

    assert (seen[0].host, seen[0].port) == ("localhost", 8000)


def test_version_prints_the_build_and_starts_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _record_serve(monkeypatch)
    stamp = tmp_path / "build.json"
    stamp.write_text(json.dumps({"version": "9.9.9", "commit": "deadbeef"}))
    monkeypatch.setattr(build_module, "BUILD_FILE", stamp)

    assert web_cli.main(["--version"]) == 0

    assert capsys.readouterr().out.strip() == "openengine 9.9.9 (deadbeef)"
    assert calls == []


def test_an_unreadable_configuration_stops_the_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert web_cli.main(["--config", str(tmp_path / "missing.toml")]) == 2

    assert "configuration file does not exist" in capsys.readouterr().err
