"""Engine's TOML configuration is singular, typed, and provider-neutral."""

from pathlib import Path

import pytest

import engine.apps.control_server.__main__ as control_server_main
import engine.apps.web.__main__ as web_main
import engine.apps.worker.__main__ as worker_main
from engine.runtime import (
    ApprovalCapability,
    EngineConfigError,
    describe_loaded_config,
    load_engine_config,
    parse_engine_config,
)


def test_defaults_allow_reads_without_selecting_a_file(tmp_path: Path) -> None:
    loaded = load_engine_config(environ={}, cwd=tmp_path)

    assert loaded.path is None
    assert loaded.config.attribution is True
    assert loaded.config.approvals.allow == (ApprovalCapability.READ,)
    assert loaded.config.approvals.auto_approve is False
    assert loaded.config.approvals.bash.allow == ()


def test_loads_provider_neutral_approval_configuration(tmp_path: Path) -> None:
    path = tmp_path / "permissions.toml"
    path.write_text(
        """
[approvals]
auto_approve = true
allow = ["read", "edit"]

[approvals.bash]
allow = ["uv run pytest **", "git status **"]
ask = ["git push **"]
deny = ["sudo **"]
""".strip()
    )

    loaded = load_engine_config(path, environ={}, cwd=tmp_path)

    assert loaded.path == path.resolve()
    assert loaded.config.attribution is True
    assert loaded.config.approvals.auto_approve is True
    assert loaded.config.approvals.allow == (
        ApprovalCapability.READ,
        ApprovalCapability.EDIT,
    )
    assert loaded.config.approvals.bash.allow == (
        "uv run pytest **",
        "git status **",
    )
    assert loaded.config.approvals.bash.ask == ("git push **",)
    assert loaded.config.approvals.bash.deny == ("sudo **",)


def test_selection_is_explicit_then_environment_then_working_directory(
    tmp_path: Path,
) -> None:
    implicit = tmp_path / "engine.toml"
    environment = tmp_path / "environment.toml"
    explicit = tmp_path / "explicit.toml"
    implicit.write_text('[approvals]\nallow = ["read"]\n')
    environment.write_text('[approvals]\nallow = ["edit"]\n')
    explicit.write_text('[approvals]\nallow = ["web"]\n')

    from_default = load_engine_config(environ={}, cwd=tmp_path)
    from_environment = load_engine_config(
        environ={"ENGINE_CONFIG": environment.name}, cwd=tmp_path
    )
    from_explicit = load_engine_config(
        explicit.name,
        environ={"ENGINE_CONFIG": environment.name},
        cwd=tmp_path,
    )

    assert from_default.path == implicit.resolve()
    assert from_default.config.approvals.allow == (ApprovalCapability.READ,)
    assert from_environment.path == environment.resolve()
    assert from_environment.config.approvals.allow == (ApprovalCapability.EDIT,)
    assert from_explicit.path == explicit.resolve()
    assert from_explicit.config.approvals.allow == (ApprovalCapability.WEB,)


@pytest.mark.parametrize(
    "document,message",
    [
        ({"approval": {}}, "unknown key in configuration: approval"),
        ({"approvals": {"automatic": True}}, "unknown key in approvals: automatic"),
        ({"approvals": {"auto_approve": "yes"}}, "must be a boolean"),
        ({"attribution": "no"}, "attribution must be a boolean"),
        ({"approvals": {"allow": "read"}}, "must be an array of strings"),
        ({"approvals": {"allow": ["Read"]}}, "unknown capability 'Read'"),
        ({"approvals": {"allow": ["read", "read"]}}, "must not contain duplicates"),
        (
            {"approvals": {"bash": {"allow": [" "]}}},
            "must not contain empty patterns",
        ),
        (
            {"approvals": {"bash": {"allowed": ["pytest **"]}}},
            "unknown key in approvals.bash: allowed",
        ),
    ],
)
def test_rejects_mistyped_or_unknown_settings(
    document: dict[str, object], message: str
) -> None:
    with pytest.raises(EngineConfigError, match=message):
        parse_engine_config(document)


def test_explicit_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(EngineConfigError, match="configuration file does not exist"):
        load_engine_config("missing.toml", environ={}, cwd=tmp_path)


def test_attribution_can_be_disabled(tmp_path: Path) -> None:
    path = tmp_path / "engine.toml"
    path.write_text("attribution = false\n")

    loaded = load_engine_config(path, environ={}, cwd=tmp_path)

    assert loaded.config.attribution is False


def test_invalid_toml_names_its_source(tmp_path: Path) -> None:
    path = tmp_path / "broken.toml"
    path.write_text("[approvals\n")

    with pytest.raises(EngineConfigError, match=f"invalid TOML in {path}"):
        load_engine_config(path, environ={}, cwd=tmp_path)


def test_startup_description_reports_the_policy_being_enforced(
    tmp_path: Path,
) -> None:
    path = tmp_path / "engine.toml"
    path.write_text(
        '[approvals]\nauto_approve = true\nallow = ["read", "mcp"]\n'
        '[approvals.bash]\nallow = ["pytest **"]\nask = ["git push **"]\n'
    )

    description = describe_loaded_config(load_engine_config(path, environ={}))

    assert str(path) in description
    assert "auto_approve=on" in description
    assert "allow=read, mcp" in description
    assert "bash_rules=2" in description
    assert "approvals enforced" in description


def test_web_entrypoint_puts_explicit_config_in_composition_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "permissions.toml"
    path.write_text('[approvals]\nauto_approve = true\nallow = ["read", "bash"]\n')
    seen = []
    monkeypatch.setattr(web_main, "report_wiring", seen.append)

    assert web_main.main(["--check", "--config", str(path)]) == 0

    assert len(seen) == 1
    assert seen[0].config_path == path.resolve()
    assert seen[0].engine_config.approvals.auto_approve is True
    assert seen[0].engine_config.approvals.allow == (
        ApprovalCapability.READ,
        ApprovalCapability.BASH,
    )


@pytest.mark.parametrize(
    "entrypoint,arguments",
    [
        (web_main.main, ["--check"]),
        (worker_main.main, []),
        (control_server_main.main, []),
    ],
)
def test_every_entrypoint_rejects_an_explicit_missing_config(
    entrypoint, arguments: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.toml"

    assert entrypoint([*arguments, "--config", str(missing)]) == 2

    assert "configuration file does not exist" in capsys.readouterr().err
