"""Engine's TOML configuration is singular and typed."""

from pathlib import Path

import pytest

import engine.apps.control_server.__main__ as control_server_main
import engine.apps.web.__main__ as web_main
import engine.apps.worker.__main__ as worker_main
from engine.runtime import (
    LoadedEngineConfig,
    ApprovalCapability,
    EngineConfigError,
    ResponseStyle,
    describe_loaded_config,
    load_engine_config,
    parse_engine_config,
)
from engine.runtime.config import DEFAULT_MODEL_TIERS, merge_model_tiers


@pytest.fixture(autouse=True)
def isolated_config_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))


def test_defaults_allow_reads_without_selecting_a_file(tmp_path: Path) -> None:
    loaded = load_engine_config(environ={}, cwd=tmp_path)

    assert loaded.path is None
    assert loaded.config.attribution is True
    assert loaded.config.default_branch == "main"
    assert loaded.config.public_url == ""
    assert loaded.config.github.repository == ""
    assert loaded.config.communications.provider == "slack"
    assert loaded.config.communications.channel == ""
    assert loaded.config.work_orders.repository == ""
    assert loaded.config.work_orders.workflow == ""
    assert loaded.config.work_orders.runner == ""
    assert loaded.config.orchestrator.host == "127.0.0.1:7233"
    assert loaded.config.orchestrator.database == ".engine/temporal.sqlite3"
    assert loaded.config.orchestrator.health_check_interval == 5.0
    assert loaded.config.claude.output_style is None
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


def test_loads_a_configured_default_branch(tmp_path: Path) -> None:
    path = tmp_path / "engine.toml"
    path.write_text('default_branch = "master"\n')

    loaded = load_engine_config(path, environ={}, cwd=tmp_path)

    assert loaded.config.default_branch == "master"


def test_loads_communications_notification_configuration(tmp_path: Path) -> None:
    path = tmp_path / "engine.toml"
    path.write_text(
        'public_url = "https://engine.example/"\n'
        '[communications]\nprovider = "slack"\nchannel = "C12345678"\n'
    )

    loaded = load_engine_config(path, environ={}, cwd=tmp_path)

    assert loaded.config.public_url == "https://engine.example"
    assert loaded.config.communications.channel == "C12345678"


def test_rejects_legacy_slack_channel_configuration(tmp_path: Path) -> None:
    path = tmp_path / "engine.toml"
    path.write_text('[slack]\nchannel_id = "C12345678"\n')

    with pytest.raises(EngineConfigError, match="unknown key in configuration: slack"):
        load_engine_config(path, environ={}, cwd=tmp_path)


def test_loads_communications_provider(tmp_path: Path) -> None:
    path = tmp_path / "engine.toml"
    path.write_text('[communications]\nprovider = "buzz"\nchannel = "engineering"\n')

    loaded = load_engine_config(path, environ={}, cwd=tmp_path)

    assert loaded.config.communications.provider == "buzz"
    assert loaded.config.communications.channel == "engineering"


def test_loads_what_a_mention_should_start(tmp_path: Path) -> None:
    path = tmp_path / "engine.toml"
    path.write_text(
        "[work_orders]\n"
        'repository = "acme/api"\n'
        'workflow = "implementation-review-v1"\n'
        'runner = "claude"\n'
        'slack_operators = ["U-operator", "U-release"]\n'
    )

    loaded = load_engine_config(path, environ={}, cwd=tmp_path)

    assert loaded.config.work_orders.repository == "acme/api"
    assert loaded.config.work_orders.workflow == "implementation-review-v1"
    assert loaded.config.work_orders.runner == "claude"
    assert loaded.config.work_orders.slack_operators == ("U-operator", "U-release")


def test_rejects_an_unknown_work_order_key(tmp_path: Path) -> None:
    path = tmp_path / "engine.toml"
    path.write_text('[work_orders]\nrepo = "acme/api"\n')

    with pytest.raises(EngineConfigError, match="unknown key in work_orders: repo"):
        load_engine_config(path, environ={}, cwd=tmp_path)


def test_loads_the_repository_webhook_deliveries_are_accepted_from(
    tmp_path: Path,
) -> None:
    path = tmp_path / "engine.toml"
    path.write_text('[github]\nrepository = "owner/name"\n[repos]\n"owner/name" = "."\n')

    loaded = load_engine_config(path, environ={}, cwd=tmp_path)

    assert loaded.config.github.repository == "owner/name"


def test_loads_github_deployment_credentials(tmp_path: Path) -> None:
    path = tmp_path / "engine.toml"
    path.write_text(
        'github_client_id = "client-from-toml"\n'
        'github_token = "token-from-toml"\n'
    )

    loaded = load_engine_config(path, environ={}, cwd=tmp_path)

    assert loaded.config.github_client_id == "client-from-toml"
    assert loaded.config.github_token == "token-from-toml"


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
    local = tmp_path / ".engine/config.toml"
    local.parent.mkdir()
    local.write_text('default_branch = "local"\n')
    from_local = load_engine_config(environ={}, cwd=tmp_path)
    assert from_local.path == local.resolve()
    assert from_local.config.default_branch == "local"
    assert from_local.config.approvals.allow == (ApprovalCapability.READ,)

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
        ({"repo_modes": {"api": "disconnected"}}, "repo_modes.api names no repository"),
        (
            {"repos": {"api": "/api"}, "repo_modes": {"api": "offline"}},
            "repos.api.mode must be one of: connected, disconnected",
        ),
        ({"trusted_repos": {"api": True}}, "trusted_repos.api names no repository"),
        (
            {"repos": {"api": "/api"}, "trusted_repos": {"api": "yes"}},
            "repos.api.trusted must be a boolean",
        ),
        ({"show_projects": False}, "unknown key in configuration: show_projects"),
        ({"approvals": {"automatic": True}}, "unknown key in approvals: automatic"),
        ({"approvals": {"auto_approve": "yes"}}, "must be a boolean"),
        ({"attribution": "no"}, "attribution must be a boolean"),
        ({"default_branch": ""}, "default_branch must be a non-empty string"),
        ({"default_branch": 1}, "default_branch must be a non-empty string"),
        ({"github_client_id": 1}, "github_client_id must be a string"),
        ({"github_token": " "}, "github_token must not be blank"),
        ({"github": {"repo": "owner/name"}}, "unknown key in github: repo"),
        ({"github": {"repository": "name"}}, 'github.repository must be "owner/name"'),
        (
            {"github": {"repository": "owner/name/extra"}},
            'github.repository must be "owner/name"',
        ),
        (
            {"github": {"repository": "owner /name"}},
            'github.repository must be "owner/name"',
        ),
        ({"github": {"repository": " "}}, "github.repository must not be blank"),
        ({"orchestrator": {"host": ""}}, "orchestrator.host must not be blank"),
        (
            {"orchestrator": {"health_check_interval": 0}},
            "orchestrator.health_check_interval must be a positive number",
        ),
        ({"output_style": "concise"}, "unknown key in configuration: output_style"),
        ({"claude": {"style": "concise"}}, "unknown key in claude: style"),
        ({"claude": {"output_style": True}}, "claude.output_style must be a string"),
        (
            {"claude": {"output_style": "Concise"}},
            "claude.output_style is unknown: 'Concise'",
        ),
        (
            {"claude": {"output_style": "terse"}},
            "expected one of: concise, explanatory, learning",
        ),
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


def test_output_style_is_engine_vocabulary_scoped_to_the_runner_that_honours_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "engine.toml"
    path.write_text('[claude]\noutput_style = "concise"\n')

    loaded = load_engine_config(path, environ={}, cwd=tmp_path)

    assert loaded.config.claude.output_style is ResponseStyle.CONCISE


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
    assert "claude.output_style=provider default" in description
    assert "default_branch=main" in description


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


def test_web_entrypoint_uses_toml_github_credentials_when_environment_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "engine.toml"
    path.write_text(
        'github_client_id = "client-from-toml"\n'
        'github_token = "token-from-toml"\n'
    )
    seen = []
    monkeypatch.delenv("GITHUB_CLIENT_ID", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(web_main, "report_wiring", seen.append)

    assert web_main.main(["--check", "--config", str(path)]) == 0

    assert seen[0].github_client_id == "client-from-toml"
    assert seen[0].github_token == "token-from-toml"


def test_web_entrypoint_prefers_environment_github_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "engine.toml"
    path.write_text(
        'github_client_id = "client-from-toml"\n'
        'github_token = "token-from-toml"\n'
    )
    seen = []
    monkeypatch.setenv("GITHUB_CLIENT_ID", "client-from-environment")
    monkeypatch.setenv("GITHUB_TOKEN", "token-from-environment")
    monkeypatch.setattr(web_main, "report_wiring", seen.append)

    assert web_main.main(["--check", "--config", str(path)]) == 0

    assert seen[0].github_client_id == "client-from-environment"
    assert seen[0].github_token == "token-from-environment"


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


def test_github_login_toml_and_secret_rotation(tmp_path, monkeypatch):
    for name in ("CLIENT_ID", "CLIENT_SECRET", "REDIRECT_URI"):
        monkeypatch.delenv(f"ENGINE_GITHUB_LOGIN_{name}", raising=False)
    (tmp_path / "engine.toml").write_text(
        'github_login_client_id = "login-client"\n'
        'github_login_redirect_uri = "https://engine.test/api/auth/github/callback"\n'
        '[github]\nrepository = "acme/api"\n[repos]\n"acme/api" = "."\n'
    )
    secret_file = tmp_path / ".env"
    secret_file.write_text('ENGINE_GITHUB_LOGIN_CLIENT_SECRET="first-${LITERAL}"\n')
    loaded = load_engine_config(environ={}, cwd=tmp_path)
    config = web_main._github_login_config(loaded)
    assert config.client_id == "login-client"
    assert config.current_secret() == "first-${LITERAL}"
    secret_file.write_text('ENGINE_GITHUB_LOGIN_CLIENT_SECRET=rotated\n')
    assert config.current_secret() == "rotated"
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_CLIENT_SECRET", "environment-secret")
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_CLIENT_ID", "environment-client")
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_REDIRECT_URI", "https://override.test/api/auth/github/callback")
    override = web_main._github_login_config(loaded)
    assert override.client_id == "environment-client"
    assert override.redirect_uri == "https://override.test/api/auth/github/callback"
    assert override.current_secret() == "environment-secret"
    monkeypatch.delenv("ENGINE_GITHUB_LOGIN_CLIENT_SECRET")
    secret_file.unlink()
    with pytest.raises(ValueError, match="secret is not configured"):
        config.current_secret()


def test_github_login_disabled_and_partial_configuration(tmp_path, monkeypatch):
    for name in ("CLIENT_ID", "CLIENT_SECRET", "REDIRECT_URI"):
        monkeypatch.delenv(f"ENGINE_GITHUB_LOGIN_{name}", raising=False)
    monkeypatch.chdir(tmp_path)
    loaded = load_engine_config(environ={}, cwd=tmp_path)
    assert web_main._github_login_config(loaded) is None
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_CLIENT_ID", "partial")
    with pytest.raises(EngineConfigError, match="requires credentials"):
        web_main._github_login_config(loaded)


@pytest.mark.parametrize("args", [[], ["--check"]])
@pytest.mark.parametrize("redirect_uri", ["", "http://public.test/api/auth/github/callback"])
def test_web_reports_invalid_login_configuration(tmp_path, monkeypatch, capsys, args, redirect_uri):
    path = tmp_path / "engine.toml"
    path.write_text("")
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_CLIENT_ID", "client")
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_CLIENT_SECRET", "private-secret")
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_REDIRECT_URI", redirect_uri)

    def unexpected_start(*args, **kwargs):
        pytest.fail("Invalid login configuration must stop startup")

    monkeypatch.setattr(web_main.uvicorn, "run", unexpected_start)
    monkeypatch.setattr(web_main, "report_wiring", unexpected_start)
    assert web_main.main(["--config", str(path), *args]) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("configuration error: GitHub login requires credentials")
    assert "Traceback" not in captured.err
    assert "private-secret" not in captured.err
    assert captured.out == ""


@pytest.mark.parametrize("args", [[], ["--check"]])
def test_web_refuses_to_start_login_without_a_repository(tmp_path, monkeypatch, capsys, args):
    """Login admits only accounts that can write to `[github] repository`, so
    without one every login would be refused: that is a setup error, reported
    at startup rather than to each user as missing access."""
    path = tmp_path / "engine.toml"
    path.write_text("")
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_CLIENT_ID", "client")
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_CLIENT_SECRET", "private-secret")
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_REDIRECT_URI", "https://engine.test/api/auth/github/callback")

    def unexpected_start(*args, **kwargs):
        pytest.fail("Login without a repository must stop startup")

    monkeypatch.setattr(web_main.uvicorn, "run", unexpected_start)
    monkeypatch.setattr(web_main, "report_wiring", unexpected_start)
    assert web_main.main(["--config", str(path), *args]) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("configuration error: GitHub login requires [github] repository")
    assert "Traceback" not in captured.err
    assert "private-secret" not in captured.err


@pytest.mark.parametrize("key", ["github_login_client_id", "github_login_redirect_uri"])
def test_github_login_config_requires_strings(key):
    with pytest.raises(EngineConfigError, match=f"{key} must be a string"):
        parse_engine_config({key: 42})


def test_github_login_secret_not_accepted_in_toml():
    with pytest.raises(EngineConfigError):
        parse_engine_config({"github_login_client_secret": "secret"})


def test_github_host_aliases_configuration():
    assert parse_engine_config({}).github.host_aliases == {}
    assert parse_engine_config({
        "github": {"host_aliases": {"Alias.Example": "Forge.Example"}}
    }).github.host_aliases == {"alias.example": "forge.example"}


@pytest.mark.parametrize("aliases", [
    "forge.example", ["forge.example"], {"alias.example": 1},
    {"": "forge.example"}, {"alias.example": " "},
])
def test_github_host_aliases_require_nonblank_string_mapping(aliases):
    with pytest.raises(EngineConfigError, match="github.host_aliases"):
        parse_engine_config({"github": {"host_aliases": aliases}})


def test_github_unbound_hosts_are_not_accepted():
    with pytest.raises(EngineConfigError, match="hosts"):
        parse_engine_config({"github": {"hosts": ["forge.example"]}})


def test_repository_choices_load_from_toml(tmp_path: Path) -> None:
    path = tmp_path / "engine.toml"
    path.write_text('[repos]\n"OpenEngine/OpenEngine" = "~/code/OpenEngine"\nn8n = "~/code/n8n"\n')
    assert load_engine_config(path, environ={}).config.repos == {
        "OpenEngine/OpenEngine": "~/code/OpenEngine", "n8n": "~/code/n8n",
    }
    assert parse_engine_config({}).repos == {}


@pytest.mark.parametrize("repos", [[], {"repo": ""}, {"repo": 123}, {" ": "/tmp/repo"}])
def test_invalid_repository_choices_are_rejected(repos) -> None:
    with pytest.raises(EngineConfigError, match="repos"):
        parse_engine_config({"repos": repos})


def test_access_operators_are_github_user_ids() -> None:
    assert parse_engine_config({"access": {"operators": [583231, 42]}}).access.operators == (583231, 42)
    assert parse_engine_config({}).access.operators == ()


@pytest.mark.parametrize("operators", [["octocat"], [0], [True], [1, 1], "42"])
def test_access_operators_reject_logins_and_bad_ids(operators) -> None:
    with pytest.raises(EngineConfigError, match="access.operators"):
        parse_engine_config({"access": {"operators": operators}})


def test_repository_projects_are_read_from_the_checkouts_remotes(
    tmp_path, *, git_repo
) -> None:
    """`[repos]` names local paths; login asks GitHub about the repository
    each one pushes to, and skips what GitHub cannot answer for."""
    remotes = {
        "api": "git@github.com:Acme/API.git",
        "web": "https://github.com/acme/web.git",
        "same": "https://github.com/acme/web",
        "gitlab": "https://gitlab.example/acme/tools.git",
        "enterprise": "https://github-web.example/acme/core.git",
    }
    repos = {}
    for name, remote in remotes.items():
        git_repo(tmp_path / name, origin=remote)
        repos[name] = str(tmp_path / name)
    repos["missing"] = str(tmp_path / "missing")
    loaded = LoadedEngineConfig(config=parse_engine_config({
        "repos": repos,
        "github": {"host_aliases": {"github-web.example": "github-api.example"}},
    }))

    projects = web_main._repository_projects(loaded)
    assert {name: projects[name] for name in repos if name in projects} == {
        "api": "acme/api", "web": "acme/web", "same": "acme/web",
        "enterprise": "github-web.example/acme/core",
    }
    assert web_main._login_repositories(loaded, projects) == (
        "acme/api", "acme/web", "github-web.example/acme/core",
    )


def test_the_servers_own_checkout_is_named_dot(
    tmp_path, monkeypatch, *, git_repo
) -> None:
    """A run in `.` is in the server's own checkout, which does not by itself
    let anyone sign in."""
    git_repo(tmp_path, origin="https://github.com/acme/server.git")
    monkeypatch.chdir(tmp_path)
    loaded = LoadedEngineConfig(config=parse_engine_config({}))

    projects = web_main._repository_projects(loaded)
    assert projects == {".": "acme/server"}
    assert web_main._login_repositories(loaded, projects) == ()


def test_web_starts_login_with_only_operators(tmp_path, monkeypatch):
    path = tmp_path / "engine.toml"
    path.write_text("[access]\noperators = [42]\n")
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_CLIENT_ID", "client")
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_CLIENT_SECRET", "private-secret")
    monkeypatch.setenv("ENGINE_GITHUB_LOGIN_REDIRECT_URI", "https://engine.test/api/auth/github/callback")

    assert web_main._github_login_config(load_engine_config(path)) is not None


def test_web_bind_address_and_state_paths_default_to_the_source_checkout_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "ENGINE_HOST", "ENGINE_PORT", "ENGINE_STATE_DIRECTORY",
        "ENGINE_SQLITE_PATH", "ENGINE_GRAPH_STATE_DIRECTORY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)

    settings = web_main._settings(load_engine_config(environ={}, cwd=tmp_path))

    assert (settings.host, settings.port) == ("localhost", 4364)
    assert Path(settings.sqlite_path) == tmp_path / "conversations.sqlite3"
    assert Path(settings.graph_state_directory) == tmp_path / "graph-state"


def test_web_state_paths_resolve_against_the_config_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "ENGINE_HOST", "ENGINE_PORT", "ENGINE_STATE_DIRECTORY",
        "ENGINE_SQLITE_PATH", "ENGINE_GRAPH_STATE_DIRECTORY",
    ):
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / "etc" / "engine.toml"
    config.parent.mkdir()
    config.write_text(
        '[server]\nhost = "127.0.0.1"\nport = 5000\n'
        '[state]\ndirectory = "state"\nsqlite_path = "chat.db"\n'
        'graph_state_directory = "graphs"\n'
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    settings = web_main._settings(load_engine_config(config))

    assert (settings.host, settings.port) == ("127.0.0.1", 5000)
    assert Path(settings.sqlite_path) == config.parent / "state" / "chat.db"
    assert Path(settings.graph_state_directory) == config.parent / "state" / "graphs"

    monkeypatch.setenv("ENGINE_HOST", "0.0.0.0")
    monkeypatch.setenv("ENGINE_PORT", "6000")
    monkeypatch.setenv("ENGINE_STATE_DIRECTORY", str(tmp_path / "var"))
    monkeypatch.setenv("ENGINE_SQLITE_PATH", "other.db")
    settings = web_main._settings(load_engine_config(config))

    assert (settings.host, settings.port) == ("0.0.0.0", 6000)
    assert Path(settings.sqlite_path) == tmp_path / "var" / "other.db"
    assert Path(settings.graph_state_directory) == tmp_path / "var" / "graphs"

    monkeypatch.setenv("ENGINE_PORT", "http")
    with pytest.raises(EngineConfigError, match="ENGINE_PORT"):
        web_main._settings(load_engine_config(config))


@pytest.mark.parametrize(
    ("host", "refused"),
    [
        ("localhost", False),
        ("127.0.0.1", False),
        ("::1", False),
        ("0.0.0.0", True),
        ("::", True),
        ("engine.example.com", True),
    ],
)
def test_web_refuses_a_non_loopback_host_without_github_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str, refused: bool
) -> None:
    for name in (
        "ENGINE_PORT", "ENGINE_STATE_DIRECTORY", "ENGINE_SQLITE_PATH",
        "ENGINE_GRAPH_STATE_DIRECTORY", "ENGINE_GITHUB_LOGIN_CLIENT_ID",
        "ENGINE_GITHUB_LOGIN_REDIRECT_URI", "ENGINE_GITHUB_LOGIN_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ENGINE_HOST", host)
    # A service token does not switch the session middleware on by itself.
    monkeypatch.setenv("ENGINE_SERVICE_TOKEN", "s" * 32)
    config = tmp_path / "engine.toml"
    config.write_text("")
    loaded = load_engine_config(config)
    settings = web_main._settings(loaded)

    if refused:
        with pytest.raises(EngineConfigError, match="GitHub login"):
            web_main._require_login_off_loopback(settings, None)
        assert web_main.main(["--config", str(config), "--check"]) == 2
    else:
        web_main._require_login_off_loopback(settings, None)
    web_main._require_login_off_loopback(settings, object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"server": {"port": "4364"}}, "server.port"),
        ({"server": {"port": 70000}}, "server.port"),
        ({"server": {"host": " "}}, "server.host"),
        ({"server": {"address": "x"}}, "unknown key in server"),
        ({"state": {"directory": ""}}, "state.directory"),
        ({"state": {"path": "x"}}, "unknown key in state"),
    ],
)
def test_server_and_state_settings_are_validated(document, message) -> None:
    with pytest.raises(EngineConfigError, match=message):
        parse_engine_config(document)


def test_the_distributable_default_config_is_loopback_and_machine_neutral() -> None:
    from engine.runtime.config import DEFAULT_CONFIG_TEMPLATE

    loaded = load_engine_config(DEFAULT_CONFIG_TEMPLATE)
    config = loaded.config

    assert config.server.host == "127.0.0.1"
    assert config.public_url == ""
    assert config.repos == {}
    assert config.github.repository == ""
    assert config.communications.channel == ""
    assert config.communications.provider == "slack"
    assert config.workflows.directory == "workflows"
    assert "[orchestrator]" not in DEFAULT_CONFIG_TEMPLATE.read_text()


def test_repo_modes_name_the_mode_of_onboarded_repositories() -> None:
    config = parse_engine_config(
        {"repos": {"api": "/api", "web": "/web"}, "repo_modes": {"api": "disconnected"}}
    )

    assert config.repo_modes == {"api": "disconnected"}


def test_trusted_repos_name_the_repositories_whose_work_orders_are_auto_approved() -> None:
    config = parse_engine_config(
        {"repos": {"api": "/api", "web": "/web"}, "trusted_repos": {"api": True, "web": False}}
    )

    assert config.trusted_repos == frozenset({"api"})


# (table, key, LoadedEngineConfig property, exported environment variable)
CONFIG_DIRECTORIES = [
    pytest.param("claude", "config_dir", "claude_config_dir", "CLAUDE_CONFIG_DIR", id="claude"),
    pytest.param("github", "gh_config_dir", "gh_config_dir", "GH_CONFIG_DIR", id="github"),
]


@pytest.mark.parametrize(("table", "key", "attribute", "variable"), CONFIG_DIRECTORIES)
@pytest.mark.parametrize("value", ["account", "~/account"])
def test_config_directory_resolves_at_load(
    tmp_path, monkeypatch, table, key, attribute, variable, value
):
    monkeypatch.setenv("HOME", str(tmp_path))
    directory = tmp_path / "account"
    directory.mkdir()
    config = tmp_path / "engine.toml"
    config.write_text(f'[{table}]\n{key} = "{value}"\n')
    monkeypatch.chdir(tmp_path.parent)

    loaded = load_engine_config(config)

    assert getattr(getattr(loaded.config, table), key) == value
    assert getattr(loaded, attribute) == directory
    assert f"{table}.{key}={directory}" in describe_loaded_config(loaded)


@pytest.mark.parametrize(("table", "key", "attribute", "variable"), CONFIG_DIRECTORIES)
@pytest.mark.parametrize("value", ["missing", "file"])
def test_config_directory_must_exist(tmp_path, table, key, attribute, variable, value):
    (tmp_path / "file").write_text("")
    config = tmp_path / "engine.toml"
    config.write_text(f'[{table}]\n{key} = "{value}"\n')
    with pytest.raises(EngineConfigError, match=f"{table}.{key}"):
        load_engine_config(config)


@pytest.mark.parametrize(("table", "key", "attribute", "variable"), CONFIG_DIRECTORIES)
@pytest.mark.parametrize("value", [1, "", " "])
def test_config_directory_settings_are_strict(table, key, attribute, variable, value):
    with pytest.raises(EngineConfigError):
        parse_engine_config({table: {key: value}})


def test_claude_settings_reject_unknown_keys():
    with pytest.raises(EngineConfigError):
        parse_engine_config({"claude": {"unknown": "x"}})


@pytest.mark.parametrize(("table", "key", "attribute", "variable"), CONFIG_DIRECTORIES)
@pytest.mark.parametrize("inherited", [None, "/existing/account"])
def test_web_preserves_login_environment_without_configuration(
    tmp_path, monkeypatch, table, key, attribute, variable, inherited
):
    import os

    monkeypatch.delenv(variable, raising=False)
    if inherited is not None:
        monkeypatch.setenv(variable, inherited)
    config = tmp_path / "engine.toml"
    config.write_text("")
    loaded, _ = web_main.read_configuration(config)
    assert getattr(loaded, attribute) is None
    assert os.environ.get(variable) == inherited


def test_web_gh_cli_uses_configured_login(tmp_path, monkeypatch):
    import os

    from engine.adapters.source_control.github.transports import gh_cli_environment

    directory = tmp_path / "gh-account"
    directory.mkdir()
    config = tmp_path / "engine.toml"
    config.write_text('[github]\ngh_config_dir = "gh-account"\n')
    monkeypatch.setenv("GH_CONFIG_DIR", "/other/gh")
    web_main.read_configuration(config)
    # monkeypatch restores the variable after the test; read_configuration set it.
    assert os.environ["GH_CONFIG_DIR"] == str(directory)
    assert gh_cli_environment()["GH_CONFIG_DIR"] == str(directory)


def test_web_claude_provider_inherits_configured_login(tmp_path, monkeypatch):
    import asyncio
    import sys

    from langgraph_acp import ClaudeACPProvider

    directory = tmp_path / "account"
    directory.mkdir()
    config = tmp_path / "engine.toml"
    config.write_text('[claude]\nconfig_dir = "account"\n')
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/other/account")
    # Use the protocol fake, but have it echo the child's inherited account.
    fake = Path(__file__).parents[1] / "langgraph-acp/tests/fake_agent.py"
    wrapper = tmp_path / "echo_account.py"
    wrapper.write_text(
        "import os, runpy\n"
        "os.environ['FAKE_AGENT_RESPONSE'] = os.environ['CLAUDE_CONFIG_DIR']\n"
        f"runpy.run_path({str(fake)!r}, run_name='__main__')\n"
    )
    web_main.read_configuration(config)

    async def prompt():
        client = await ClaudeACPProvider(command=(sys.executable, str(wrapper))).connect()
        try:
            session = await client.new_session(cwd=tmp_path)
            return [event async for event in session.prompt("Which account?")]
        finally:
            await client.close()

    events = asyncio.run(prompt())
    assert any(event.data.get("content") == {"type": "text", "text": str(directory)} for event in events)


@pytest.mark.parametrize("backend", ["process", "smolvm"])
def test_sandbox_configuration(backend):
    assert parse_engine_config({}).sandbox.backend == "process"
    assert parse_engine_config({"sandbox": {"backend": backend}}).sandbox.backend == backend


@pytest.mark.parametrize("sandbox", [{"backend": "docker"}, {"backend": None}, {"unknown": True}, "process"])
def test_invalid_sandbox_configuration(sandbox):
    with pytest.raises(EngineConfigError, match="sandbox"):
        parse_engine_config({"sandbox": sandbox})


@pytest.mark.parametrize("app", [web_main, worker_main, control_server_main])
def test_sandbox_backend_composition(app, tmp_path):
    from dataclasses import replace
    from engine.adapters.sandbox.process import ProcessSandbox
    from engine.adapters.sandbox.smolvm import SmolvmSandbox

    settings = app.Settings()
    if hasattr(settings, "sqlite_path"):
        settings = replace(settings, sqlite_path=str(tmp_path / "state.sqlite"))
    assert isinstance(app.build_capabilities(settings).sandbox, ProcessSandbox)
    settings = replace(settings, engine_config=parse_engine_config({"sandbox": {"backend": "smolvm"}}))
    assert isinstance(app.build_capabilities(settings).sandbox, SmolvmSandbox)


def test_sandbox_image_configuration():
    assert parse_engine_config({"sandbox": {"image": " ./guest.tar "}}).sandbox.image == "./guest.tar"
    with pytest.raises(EngineConfigError, match="sandbox.image"):
        parse_engine_config({"sandbox": {"image": 42}})


def test_github_repositories_combine_with_legacy_repository():
    config = parse_engine_config({
        "github": {"repository": "Acme/API", "repositories": ["acme/api", "other/web"]},
        "repos": {"acme/api": "/api", "Other/Web": "/web"},
    })
    assert config.github.webhook_repositories == ("acme/api", "other/web")


@pytest.mark.parametrize("repositories", ["acme/api", [""], ["invalid"], [12]])
def test_github_repositories_reject_invalid_lists(repositories):
    with pytest.raises(EngineConfigError, match="github.repositories"):
        parse_engine_config({"github": {"repositories": repositories}})


@pytest.mark.parametrize("github", [
    {"repository": "acme/api"}, {"repositories": ["acme/api"]},
])
def test_github_repositories_require_checkout_mappings(github):
    with pytest.raises(EngineConfigError, match="acme/api.*checkout path under"):
        parse_engine_config({"github": github, "repos": {"other/repo": "/other"}})


@pytest.mark.parametrize("xdg", [None, "absolute", "relative"])
def test_machine_configuration_precedes_checkout_without_merging(tmp_path, xdg):
    config_home = tmp_path / "custom" if xdg == "absolute" else Path.home() / ".config"
    machine = config_home / "openengine" / "engine.toml"
    machine.parent.mkdir(parents=True)
    machine.write_text(
        '[github]\nrepositories = ["other/web"]\n'
        '[repos]\n"other/web" = "/srv/web"\n'
    )
    (tmp_path / "engine.toml").write_text('[repos]\n"checkout/repo" = "/checkout"\n')
    local = tmp_path / ".engine/config.toml"
    local.parent.mkdir()
    local.write_text('[repos]\n"local/repo" = "/local"\n')
    environment = {} if xdg is None else {
        "XDG_CONFIG_HOME": str(config_home) if xdg == "absolute" else "relative"
    }
    loaded = load_engine_config(environ=environment, cwd=tmp_path)
    assert loaded.path == machine.resolve()
    assert loaded.config.repos == {"other/web": "/srv/web"}
    assert loaded.config.github.webhook_repositories == ("other/web",)

    override = tmp_path / "override.toml"
    override.write_text('[repos]\n"explicit/repo" = "/explicit"\n')
    environment["ENGINE_CONFIG"] = str(override)
    assert load_engine_config(environ=environment, cwd=tmp_path).path == override
    assert load_engine_config(machine, environ=environment, cwd=tmp_path).path == machine
    assert load_engine_config(local, environ=environment, cwd=tmp_path).path == local


def test_invalid_machine_config_does_not_fall_back_to_checkout(tmp_path):
    machine = Path.home() / ".config" / "openengine" / "engine.toml"
    machine.parent.mkdir(parents=True)
    machine.write_text('[github]\nrepositories = ["missing/checkout"]\n')
    (tmp_path / "engine.toml").write_text("")
    with pytest.raises(EngineConfigError, match="missing/checkout"):
        load_engine_config(environ={}, cwd=tmp_path)


def test_invalid_local_config_does_not_fall_back_to_root(tmp_path):
    (tmp_path / "engine.toml").write_text("[repos]\n")
    local = tmp_path / ".engine/config.toml"
    local.parent.mkdir()
    local.write_text("[repos")
    with pytest.raises(EngineConfigError, match="invalid TOML"):
        load_engine_config(environ={}, cwd=tmp_path)


def test_local_config_works_without_root_config(tmp_path):
    local = tmp_path / ".engine/config.toml"
    local.parent.mkdir()
    local.write_text(
        '[repos]\n"owner/repo" = "~/code/repo"\n'
        '[workflows]\ndirectory = "../workflows"\n'
        '[approvals.bash]\nallow = ["uv run pytest"]\n'
    )
    loaded = load_engine_config(environ={}, cwd=tmp_path)
    assert loaded.path == local.resolve()
    assert loaded.config.repos == {"owner/repo": "~/code/repo"}
    assert loaded.workflows_directory == (tmp_path / "workflows").resolve()
    assert loaded.config.approvals.bash.allow == ("uv run pytest",)


def test_onboarding_directory_resolves_against_config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = tmp_path / "deploy" / "engine.toml"
    config.parent.mkdir(parents=True)
    config.write_text('[onboarding]\ndirectory = "../checkouts"\n')
    loaded = load_engine_config(config, environ={}, cwd=tmp_path)
    assert loaded.config.onboarding.directory == "../checkouts"
    assert loaded.onboarding_directory == (tmp_path / "checkouts").resolve()


def test_onboarding_directory_default_is_home_code(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = tmp_path / "engine.toml"
    config.write_text("")
    loaded = load_engine_config(config, environ={}, cwd=tmp_path)
    assert loaded.config.onboarding.directory == ""
    assert loaded.onboarding_directory == (tmp_path / "home" / "code").resolve()


@pytest.mark.parametrize("value", ["   "])
def test_onboarding_directory_blank_is_rejected(value):
    with pytest.raises(EngineConfigError, match="must not be blank"):
        parse_engine_config({"onboarding": {"directory": value}})


def test_onboarding_directory_must_be_a_string():
    with pytest.raises(EngineConfigError, match="must be a string"):
        parse_engine_config({"onboarding": {"directory": 42}})

def test_review_resolution_configuration():
    assert parse_engine_config({}).github.resolve_addressed_threads is True
    assert parse_engine_config({"github": {"resolve_addressed_threads": False}}).github.resolve_addressed_threads is False
    with pytest.raises(EngineConfigError, match="must be a boolean"):
        parse_engine_config({"github": {"resolve_addressed_threads": "false"}})


def test_review_config_defaults_and_repository_overrides(tmp_path):
    defaults = parse_engine_config({})
    assert defaults.github.review_request_workflow == 'review'
    assert defaults.pull_requests.auto_approve_green is False
    config = parse_engine_config({
        'repos': {'api': str(tmp_path)},
        'pull_requests': {'auto_approve_green': True, 'repos': {'api': False}},
        'github': {'review_request_workflow': 'custom-review'},
    })
    assert config.github.review_request_workflow == 'custom-review'
    assert config.pull_requests.default_for('api', config.repos) == 'false'
    assert config.pull_requests.default_for(str(tmp_path), config.repos) == 'false'
    assert config.pull_requests.default_for('another/repo', config.repos) == 'true'


@pytest.mark.parametrize('document', [
    {'pull_requests': {'auto_approve_green': 'true'}},
    {'pull_requests': {'unknown': True}},
    {'pull_requests': {'repos': {'missing': True}}},
    {'repos': {'api': '.'}, 'pull_requests': {'repos': {'api': 1}}},
    {'github': {'review_request_workflow': ''}},
    {'github': {'review_request_workflow': False}},
    {'github': {'review_workflow': 'review'}},
])
def test_invalid_review_configuration(document):
    with pytest.raises(EngineConfigError):
        parse_engine_config(document)

def test_nested_repository_settings_and_opt_out_webhooks():
    config = parse_engine_config({
        'repos': {
            'OpenEngine/OpenEngine': {'path': '~/code/OpenEngine', 'default': True},
            'OpenEngine/langchain-s1': {'path': '/langchain', 'trusted': True},
            'owner/private': {'path': '/private', 'mode': 'disconnected',
                              'trusted': False, 'auto_approve_green': False, 'webhooks': False},
            'n8n': {'path': '/n8n', 'webhooks': True},
        },
        'pull_requests': {'auto_approve_green': True},
    })
    assert config.github.webhook_repositories == ('openengine/openengine', 'openengine/langchain-s1')
    assert config.work_orders.repository == '~/code/OpenEngine'
    assert config.repo_modes == {'owner/private': 'disconnected'}
    assert config.repository_trust == {'OpenEngine/langchain-s1': True, 'owner/private': False}
    assert config.trusted_repos == {'OpenEngine/langchain-s1'}
    assert config.pull_requests.repos == {'owner/private': False}
    assert config.pull_requests.default_for('owner/private', config.repos) == 'false'
    assert config.repo_configs['n8n'].webhooks is False
    assert config.repo_configs['OpenEngine/OpenEngine'].trusted is None


@pytest.mark.parametrize('entry', [
    {}, {'path': ''}, {'path': 1}, {'path': '.', 'unknown': True},
    {'path': '.', 'mode': 'offline'}, {'path': '.', 'mode': None},
    {'path': '.', 'mode': 'connected'},
    *({'path': '.', key: 1} for key in ('trusted', 'auto_approve_green', 'webhooks', 'default')),
])
def test_invalid_nested_repository_settings(entry):
    with pytest.raises(EngineConfigError, match='repos'):
        parse_engine_config({'repos': {'owner/repo': entry}})


def test_multiple_default_repositories_rejected():
    with pytest.raises(EngineConfigError, match='at most one'):
        parse_engine_config({'repos': {
            'one': {'path': '/one', 'default': True},
            'two': {'path': '/two', 'default': True},
        }})


@pytest.mark.parametrize('legacy, key, value, warning', [
    ({'repo_modes': {'owner/repo': 'disconnected'}}, 'mode', 'disconnected', 'repo_modes'),
    ({'trusted_repos': {'owner/repo': True}}, 'trusted', True, 'trusted_repos'),
    ({'pull_requests': {'repos': {'owner/repo': False}}}, 'auto_approve_green', False, 'pull_requests.repos'),
    ({'github': {'repository': 'Owner/Repo'}}, 'webhooks', True, 'github.repository'),
    ({'github': {'repositories': ['owner/repo']}}, 'webhooks', True, 'github.repositories'),
    ({'work_orders': {'repository': 'owner/repo'}}, 'default', True, 'work_orders.repository'),
])
def test_legacy_repository_settings_warn_merge_and_reject_conflicts(caplog, legacy, key, value, warning):
    config = parse_engine_config({'repos': {'owner/repo': '/repo'}, **legacy})
    assert getattr(config.repo_configs['owner/repo'], key) == value
    assert warning in caplog.text
    assert '[repos."owner/name"]' in caplog.text
    assert 'deprecated' in caplog.text
    # Identical explicit values can coexist during migration.
    parse_engine_config({'repos': {'owner/repo': {'path': '/repo', key: value}}, **legacy})
    different = 'connected' if key == 'mode' else not value
    if key == 'mode':
        legacy = {'repo_modes': {'owner/repo': different}}
        different = value
    with pytest.raises(EngineConfigError, match='conflicts'):
        parse_engine_config({'repos': {'owner/repo': {'path': '/repo', key: different}}, **legacy})


@pytest.mark.parametrize('repository', ['.', 'repo', '~/checkout'])
def test_legacy_default_matches_name_or_resolved_path(tmp_path, monkeypatch, repository):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('HOME', str(tmp_path))
    path = str(tmp_path / 'checkout') if repository == '~/checkout' else str(tmp_path)
    config = parse_engine_config({'repos': {'repo': {'path': path}},
                                  'work_orders': {'repository': repository}})
    assert config.repo_configs['repo'].default is True
    assert config.work_orders.repository == path


def test_unmatched_legacy_default_preserved_and_conflicting_default_rejected():
    document = {'repos': {'repo': {'path': '/repo'}}, 'work_orders': {'repository': '/other'}}
    assert parse_engine_config(document).work_orders.repository == '/other'
    document['repos']['repo']['default'] = True
    with pytest.raises(EngineConfigError, match='conflicts'):
        parse_engine_config(document)
    document['repos']['other'] = {'path': '/other'}
    with pytest.raises(EngineConfigError, match='conflicts'):
        parse_engine_config(document)


def test_legacy_github_allowlist_does_not_exclude_other_repositories():
    config = parse_engine_config({'repos': {'one/repo': '.', 'two/repo': '/two'},
                                  'github': {'repository': 'one/repo'}})
    assert config.github.webhook_repositories == ('one/repo', 'two/repo')


def test_legacy_merge_cannot_hide_invalid_new_boolean():
    with pytest.raises(EngineConfigError):
        parse_engine_config({'repos': {'repo': {'path': '.', 'trusted': 1}},
                             'trusted_repos': {'repo': True}})


def test_connected_mode_requires_omission_in_new_repository_tables():
    config = parse_engine_config({'repos': {'owner/repo': {'path': '.'}}})
    assert config.repo_configs['owner/repo'].mode is None
    assert config.repo_modes == {}
    with pytest.raises(EngineConfigError, match='omit mode for connected repositories'):
        parse_engine_config({'repos': {'owner/repo': {'path': '.', 'mode': 'connected'}},
                             'repo_modes': {'owner/repo': 'connected'}})


def test_legacy_connected_repository_mode_remains_supported(caplog):
    config = parse_engine_config({'repos': {'owner/repo': '.'},
                                  'repo_modes': {'owner/repo': 'connected'}})
    assert config.repo_modes == {'owner/repo': 'connected'}
    assert 'repo_modes is deprecated' in caplog.text

@pytest.mark.parametrize("project,workflow,accepted", [
    (None, "tdd-guide", False),
    # `oe graph add` with no project, or the repository's own project.
    ("default", "tdd-guide", True),
    ("acme/api", "tdd-guide", True),
    # Another project's graph is not this repository's, by name...
    ("sandbox", "tdd-guide", False),
    # ...unless the deployment names it outright.
    ("sandbox", "g-1", True),
    # A refusal other than "no such graph" is reported as itself.
    ("default", "tdd-guide@latest", False),
])
def test_web_check_reports_a_work_order_workflow_that_names_nothing(
    tmp_path, monkeypatch, capsys, project, workflow, accepted,
):
    """`work_orders.workflow` names a workflow or a graph registered with `oe graph add`.

    One that names neither, for the repositories webhooks start work in, is
    refused at startup, with the name, rather than when the first assignment
    or mention cannot start anything.
    """
    from engine.graph_runtime_langgraph.workflows import RUNS
    from engine.graph_service.store import GraphRow, GraphServiceStore, VersionRow

    path = tmp_path / "engine.toml"
    path.write_text(
        f'[github]\nrepositories = ["acme/api"]\n[repos]\n"acme/api" = "{tmp_path}"\n[work_orders]\nworkflow = "{workflow}"\n'
    )
    graph_state = tmp_path / "graph-state"
    graph_state.mkdir()
    monkeypatch.setenv("ENGINE_GRAPH_STATE_DIRECTORY", str(graph_state))
    if project is not None:
        store = GraphServiceStore(graph_state / RUNS)
        store.insert_graph(GraphRow("g-1", project, "tdd-guide", "gv-1", "now", "now"))
        store.insert_version(VersionRow("gv-1", "g-1", 1, {}, "digest", "now"))
        store.close()
    seen = []
    monkeypatch.setattr(web_main, "report_wiring", seen.append)

    assert web_main.main(["--check", "--config", str(path)]) == (0 if accepted else 2)

    if accepted:
        assert len(seen) == 1
    else:
        assert not seen
        assert f"work_orders.workflow is '{workflow}'" in capsys.readouterr().err


def test_runner_path_and_model_tiers_parse(monkeypatch) -> None:
    monkeypatch.setenv("HOME", "/home/operator")

    config = parse_engine_config({
        "runners": {
            "codex": {
                "path": "~/.local/bin/codex",
                "models": {"cheapest": "", "default": "gpt-5", "elevated": "gpt-5-pro"},
            },
        },
    })

    assert config.runner_paths == {"codex": "/home/operator/.local/bin/codex"}
    assert config.model_tiers == {"codex": {"cheapest": "", "default": "gpt-5", "elevated": "gpt-5-pro"}}


def test_no_runners_table_leaves_runners_to_their_defaults() -> None:
    config = parse_engine_config({})

    assert config.runner_paths == {}
    assert merge_model_tiers(config.model_tiers) == DEFAULT_MODEL_TIERS


def test_configured_tiers_merge_over_the_defaults() -> None:
    config = parse_engine_config({
        "runners": {"claude": {"models": {"elevated": "claude-opus-9"}}, "qwen": {"models": {"default": "qwen3"}}},
    })

    tiers = merge_model_tiers(config.model_tiers)

    assert tiers["claude"] == {"cheapest": "haiku", "default": "sonnet", "elevated": "claude-opus-9"}
    assert tiers["codex"] == DEFAULT_MODEL_TIERS["codex"]
    assert tiers["qwen"] == {"default": "qwen3"}


@pytest.mark.parametrize(
    ("runner", "message"),
    [
        ({"path": ""}, "runners.codex.path must not be blank"),
        ({"path": "  "}, "runners.codex.path must not be blank"),
        ({"path": 7}, "runners.codex.path must be a string"),
        ({"binary": "/opt/codex"}, "unknown key in runners.codex: binary"),
        ({"models": {"default": "  "}}, "runners.codex.models.default must not be blank"),
    ],
)
def test_runner_settings_reject_bad_values(runner: dict[str, object], message: str) -> None:
    with pytest.raises(EngineConfigError, match=message):
        parse_engine_config({"runners": {"codex": runner}})
