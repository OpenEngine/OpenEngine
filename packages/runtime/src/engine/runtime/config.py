"""Configuration loaded before applications compose adapters.

This module deliberately stops at reading and validating Engine's vocabulary.
Runners expose provider translators for that vocabulary, while policy
evaluation remains a separate concern.
"""

from __future__ import annotations

import logging
import os
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from engine.domain import ForgeMode
from engine.ports.agent_runner import ResponseStyle
from engine.ports.permissions import ApprovalCapability
from engine.runtime.terminal_mcp import REPOSITORY_TOOL_NAMES

CONFIG_ENVIRONMENT_VARIABLE = "ENGINE_CONFIG"
DEFAULT_CONFIG_NAME = "engine.toml"
DEFAULT_CONFIG_TEMPLATE = Path(__file__).with_name("default-engine.toml")
#: What a repository table's `mode` may say a repository's WorkOrders run as.
REPO_MODES = tuple(str(mode) for mode in ForgeMode)
"""A distributable `engine.toml`: loopback only, nothing machine-specific."""
#: What `model: default`, `model: elevated` and `model: cheapest` mean when a
#: backend says nothing. A blank tier leaves the agent to its own default, for
#: Codex the model `~/.codex/config.toml` names.
DEFAULT_MODEL_TIERS: Mapping[str, Mapping[str, str]] = {
    "claude": {"cheapest": "haiku", "default": "sonnet", "elevated": "opus"},
    "codex": {"cheapest": "", "default": "", "elevated": ""},
    "opencode": {"cheapest": "", "default": "", "elevated": ""},
}


def merge_model_tiers(
    model_tiers: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, dict[str, str]]:
    """`[runners.<runner>.models]` over `DEFAULT_MODEL_TIERS`, tier by tier."""
    return {
        runner: {**DEFAULT_MODEL_TIERS.get(runner, {}), **models}
        for runner, models in {**DEFAULT_MODEL_TIERS, **(model_tiers or {})}.items()
    }


class EngineConfigError(ValueError):
    """A configuration file could not be found, parsed, or validated."""


@dataclass(frozen=True, slots=True)
class BashApprovalConfig:
    """Shell patterns grouped by the decision they will eventually produce."""

    allow: tuple[str, ...] = ()
    ask: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ApprovalConfig:
    """Approval policy expressed without naming a provider or provider tool."""

    auto_approve: bool = False
    allow: tuple[ApprovalCapability, ...] = (ApprovalCapability.READ,)
    bash: BashApprovalConfig = BashApprovalConfig()


@dataclass(frozen=True, slots=True)
class WorkflowsConfig:
    """Where trusted repository-owned Python workflow definitions live."""

    directory: str = ""


@dataclass(frozen=True, slots=True)
class OnboardingConfig:
    """Where `oe repo add` clones a repository when no path is given.

    `directory`, when set, replaces the daemon home's `code` subdirectory, so
    a deployment that keeps checkouts elsewhere need not pass `--path` on every
    call. An explicit checkout path and an existing checkout still win.
    """

    directory: str = ""


#: What `oe agent claude|codex|opencode` serves when `[sessions] tools` says nothing: an implementation node's tools.
DEFAULT_SESSION_TOOLS: tuple[str, ...] = ("git_subcommand", "open_pull_request")


@dataclass(frozen=True, slots=True)
class SessionsConfig:
    """`[sessions]`: what an interactive `oe agent claude|codex|opencode` session is given."""

    tools: tuple[str, ...] = DEFAULT_SESSION_TOOLS
    """The repository tools its agent reaches through Engine, as an implementation node's `tools`."""


@dataclass(frozen=True, slots=True)
class GraphsConfig:
    """How this backend treats graphs registered through `oe graph add`."""

    allow_python: bool = True
    """Whether a Python graph may be uploaded. Python runs with the daemon's
    privileges, so a backend that wants YAML only turns this off."""


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """Where the web interface listens."""

    host: str = "localhost"
    port: int = 4364


@dataclass(frozen=True, slots=True)
class StateConfig:
    """Where the web interface keeps its databases.

    `directory` resolves against the configuration file's directory, and the
    two paths inside it against `directory`, so one setting moves them all.
    """

    directory: str = "."
    sqlite_path: str = "conversations.sqlite3"
    graph_state_directory: str = "graph-state"


@dataclass(frozen=True, slots=True)
class OrchestratorConfig:
    """Settings for the local Temporal service owned by the orchestrator."""

    host: str = "127.0.0.1:7233"
    database: str = ".engine/temporal.sqlite3"
    health_check_interval: float = 5.0


@dataclass(frozen=True, slots=True)
class ClaudeConfig:
    """Settings that only apply when Claude Code is the runner.

    A table of its own because these have no counterpart elsewhere: written at
    the top level they would read as promises Engine cannot keep for every
    provider, and a reader could not tell which of the two they were.
    """

    config_dir: str = ""
    """Claude Code login directory, or empty to inherit the provider environment."""

    output_style: ResponseStyle | None = None
    """How Claude should write, or ``None`` to leave its own default.

    Still Engine's vocabulary rather than Claude's spelling -- the adapter owns
    that translation -- but scoped to the one runner that can honour it.
    """


@dataclass(frozen=True, slots=True)
class CommunicationsConfig:
    """Selects the adapter that fulfills the communications capability."""

    provider: str = "slack"
    channel: str = ""


@dataclass(frozen=True, slots=True)
class PullRequestsConfig:
    """Defaults for review graph inputs, independent of tool approval policy."""

    auto_approve_green: bool = False
    repos: Mapping[str, bool] = field(default_factory=dict)

    def default_for(self, repository: str, repos: Mapping[str, str]) -> str:
        name = repository
        if name not in repos:
            checkout = Path(repository).expanduser().resolve()
            name = next((name for name, path in repos.items()
                         if checkout == Path(path).expanduser().resolve()), repository)
        return str(self.repos.get(name, self.auto_approve_green)).lower()


@dataclass(frozen=True, slots=True)
class GitHubConfig:
    """Which repositories on GitHub this deployment answers events from.

    The shared secret that signs those deliveries is deliberately absent: like
    the login client secret it belongs in a server-local `.env` beside this
    file, because `engine.toml` is checked in and a secret in it is published
    the moment it is committed. Repository webhook names are derived from
    `[repos]`, keeping deployment settings out of the handler's source.
    """

    review_request_workflow: str = "review"
    resolve_addressed_threads: bool = True
    repository: str = ""
    """`owner/name` of the repository whose webhooks are accepted, or empty.

    Together with `repositories`, this forms the webhook allowlist. An empty
    allowlist leaves the route unconfigured rather than accepting any repository.
    """

    host_aliases: Mapping[str, str] = field(default_factory=dict)
    """Web authorities mapped to their GitHub transport authority, including ports."""

    repositories: tuple[str, ...] = ()
    """Additional repositories whose webhooks are accepted."""

    gh_config_dir: str = ""
    """`gh` login directory (GH_CONFIG_DIR), or empty to inherit the provider environment."""

    ssh_key: str = ""
    """SSH private key for daemon git operations, or empty to inherit SSH settings."""

    @property
    def webhook_repositories(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(
            repo.lower() for repo in (self.repository, *self.repositories) if repo
        ))


@dataclass(frozen=True, slots=True)
class AccessConfig:
    """Who may use the web interface beyond what repository access grants."""

    operators: tuple[int, ...] = ()
    """GitHub user IDs admitted without a repository permission check.

    IDs rather than logins, because a login can be renamed and then claimed by
    somebody else. Operators can sign in before they have write access anywhere,
    and while the server's own `gh` login cannot answer permission checks.
    """


@dataclass(frozen=True, slots=True)
class WorkOrdersConfig:
    """What a work order gets when nobody filled in a form to ask for one.

    Starting one from a chat message means starting it from a sentence, so the
    three answers the web form collects alongside the prompt have to come from
    somewhere. `repository` has no sensible default and is what makes the
    feature available at all: without it a mention is answered with a note
    saying so rather than with a run against a repository nobody named.
    """

    repository: str = ""
    workflow: str = ""
    """Which workflow to run, or empty for the deployment's only one."""
    runner: str = ""
    """Which agent runs it, or empty for the executor's default."""
    slack_operators: tuple[str, ...] = ()
    """Slack user IDs allowed to control WorkOrders started by other people."""


@dataclass(frozen=True, slots=True)
class SandboxConfig:
    backend: str = "process"
    image: str = ""


@dataclass(frozen=True, slots=True)
class RepoConfig:
    """Settings owned by one configured checkout."""

    path: str
    mode: str | None = None
    trusted: bool | None = None
    auto_approve_green: bool | None = None
    webhooks: bool = True
    default: bool = False


@dataclass(frozen=True, slots=True)
class EngineConfig:
    """All configuration understood by this version of Engine."""

    default_branch: str = "main"
    github_client_id: str = ""
    github_login_client_id: str = ""
    github_login_redirect_uri: str = ""
    github_token: str = ""
    public_url: str = ""
    server: ServerConfig = ServerConfig()
    state: StateConfig = StateConfig()
    github: GitHubConfig = GitHubConfig()
    pull_requests: PullRequestsConfig = field(default_factory=PullRequestsConfig)
    access: AccessConfig = AccessConfig()
    communications: CommunicationsConfig = CommunicationsConfig()
    work_orders: WorkOrdersConfig = WorkOrdersConfig()
    approvals: ApprovalConfig = ApprovalConfig()
    workflows: WorkflowsConfig = WorkflowsConfig()
    orchestrator: OrchestratorConfig = OrchestratorConfig()
    claude: ClaudeConfig = ClaudeConfig()
    sandbox: SandboxConfig = SandboxConfig()
    graphs: GraphsConfig = GraphsConfig()
    sessions: SessionsConfig = SessionsConfig()
    onboarding: OnboardingConfig = OnboardingConfig()
    model_tiers: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    """`[runners.<runner>.models]`: tier name to model, per runner.

    What a graph's `model: elevated` means on this backend, so graphs name a
    tier and each backend decides which model that is. A blank tier leaves
    the agent to its own default; `merge_model_tiers` fills in the rest.
    """
    runner_paths: Mapping[str, str] = field(default_factory=dict)
    """`[runners.<runner>.path]`: the agent binary a runner launches, `~` expanded."""
    attribution: bool = True
    repo_configs: Mapping[str, RepoConfig] = field(default_factory=dict)
    repos: Mapping[str, str] = field(default_factory=dict)
    repo_modes: Mapping[str, str] = field(default_factory=dict)
    """`[repos]` names mapped to the mode their WorkOrders run in.

    Written by `oe repo add`. A repository named `disconnected` here has every
    WorkOrder on it run disconnected; one not named runs as the workflow says.
    """
    repository_trust: Mapping[str, bool] = field(default_factory=dict)
    trusted_repos: frozenset[str] = frozenset()
    """`[repos]` names whose WorkOrders are auto-approved.

    Configured explicitly when approvals are left to trusted repositories:
    `approvals.auto_approve` covers every repository, this only the ones named.
    """


@dataclass(frozen=True, slots=True)
class LoadedEngineConfig:
    """Validated settings together with the file they came from, if any."""

    config: EngineConfig = EngineConfig()
    path: Path | None = None
    # Distinguish operator overrides from automatically discovered project files.
    explicit: bool = False

    @property
    def _base_directory(self) -> Path:
        return self.path.parent if self.path is not None else Path.cwd()

    def _config_directory(self, configured: str) -> Path | None:
        if not configured:
            return None
        return _relative_to(Path(configured).expanduser(), self._base_directory).resolve()

    @property
    def claude_config_dir(self) -> Path | None:
        return self._config_directory(self.config.claude.config_dir)

    @property
    def gh_config_dir(self) -> Path | None:
        return self._config_directory(self.config.github.gh_config_dir)

    @property
    def ssh_key(self) -> Path | None:
        return self._config_directory(self.config.github.ssh_key)

    @property
    def workflows_directory(self) -> Path | None:
        configured = self.config.workflows.directory
        if not configured:
            return None
        return _relative_to(Path(configured), self._base_directory).resolve()

    @property
    def orchestrator_database(self) -> Path:
        """Resolve the Temporal database beside the selected configuration."""
        return _relative_to(
            Path(self.config.orchestrator.database), self._base_directory
        ).resolve()

    @property
    def onboarding_directory(self) -> Path:
        """Parent directory new checkouts are cloned into when no path is given.

        `[onboarding] directory`, when set, resolves against the configuration
        file's directory (like `state.directory`), else the working directory;
        empty falls back to `~/code`, the historical default.
        """
        configured = self.config.onboarding.directory
        if not configured:
            return (Path.home() / "code").resolve()
        return _relative_to(
            Path(configured).expanduser(), self._base_directory
        ).resolve()


def load_engine_config(
    explicit_path: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
) -> LoadedEngineConfig:
    """Load the selected TOML file, or return defaults when none is selected.

    Selection is intentionally singular: an explicit path wins over
    ``ENGINE_CONFIG``, then the machine configuration at
    ``$XDG_CONFIG_HOME/openengine/engine.toml`` (defaulting to
    ``~/.config/openengine/engine.toml``), then ``.engine/config.toml`` and
    ``engine.toml`` in the current directory.
    Files are not merged, so the effective permission policy always has one
    inspectable source.
    """

    environment = os.environ if environ is None else environ
    directory = Path.cwd() if cwd is None else Path(cwd)
    selected: Path | None

    if explicit_path is not None:
        selected = _relative_to(Path(explicit_path), directory)
    elif configured := environment.get(CONFIG_ENVIRONMENT_VARIABLE):
        selected = _relative_to(Path(configured), directory)
    else:
        xdg_config = environment.get("XDG_CONFIG_HOME", "")
        config_home = (
            Path(xdg_config) if xdg_config and Path(xdg_config).is_absolute()
            else Path.home() / ".config"
        )
        machine = config_home / "openengine" / DEFAULT_CONFIG_NAME
        default = directory / DEFAULT_CONFIG_NAME
        local = directory / ".engine" / "config.toml"
        selected = next(
            (path for path in (machine, local, default) if path.is_file()), None
        )

    if selected is None:
        return LoadedEngineConfig()

    path = selected.resolve()
    try:
        with path.open("rb") as config_file:
            document = tomllib.load(config_file)
    except FileNotFoundError as error:
        raise EngineConfigError(f"configuration file does not exist: {path}") from error
    except OSError as error:
        raise EngineConfigError(f"cannot read configuration file {path}: {error}") from error
    except tomllib.TOMLDecodeError as error:
        raise EngineConfigError(f"invalid TOML in {path}: {error}") from error

    loaded = LoadedEngineConfig(
        config=parse_engine_config(document), path=path,
        explicit=explicit_path is not None or bool(environment.get(CONFIG_ENVIRONMENT_VARIABLE)),
    )
    for label, directory in (
        ("claude.config_dir", loaded.claude_config_dir),
        ("github.gh_config_dir", loaded.gh_config_dir),
    ):
        if directory is not None and not directory.is_dir():
            raise EngineConfigError(f"{label} is not an existing directory: {directory}")
    if loaded.ssh_key is not None and not loaded.ssh_key.is_file():
        raise EngineConfigError(f"github.ssh_key is not an existing file: {loaded.ssh_key}")
    return loaded


def parse_engine_config(document: Mapping[str, object]) -> EngineConfig:
    """Validate a decoded TOML document and return immutable settings."""

    _reject_unknown(
        document,
        {
            "access",
            "attribution",
            "approvals",
            "claude",
            "communications",
            "default_branch",
            "github",
            "github_client_id",
            "github_login_client_id",
            "github_login_redirect_uri",
            "github_token",
            "graphs",
            "onboarding",
            "orchestrator",
            "public_url",
            "pull_requests",
            "repo_modes",
            "repos",
            "runners",
            "trusted_repos",
            "server",
            "sandbox",
            "sessions",
            "state",
            "work_orders",
            "workflows",
        },
        "configuration",
    )
    sandbox = _table(document.get("sandbox", {}), "sandbox")
    _reject_unknown(sandbox, {"backend", "image"}, "sandbox")
    sandbox_backend = sandbox.get("backend", "process")
    if sandbox_backend not in ("process", "smolvm"):
        raise EngineConfigError("sandbox.backend must be process or smolvm")
    sandbox_image = sandbox.get("image", "")
    if not isinstance(sandbox_image, str):
        raise EngineConfigError("sandbox.image must be a string")

    attribution = document.get("attribution", True)
    if not isinstance(attribution, bool):
        raise EngineConfigError("attribution must be a boolean")

    default_branch = document.get("default_branch", "main")
    if not isinstance(default_branch, str) or not default_branch.strip():
        raise EngineConfigError("default_branch must be a non-empty string")

    github_client_id = _optional_nonblank_string(
        document.get("github_client_id", ""), "github_client_id"
    )
    github_token = _optional_nonblank_string(
        document.get("github_token", ""), "github_token"
    )
    public_url = _optional_nonblank_string(document.get("public_url", ""), "public_url")

    github = _table(document.get("github", {}), "github")
    _reject_unknown(
        github,
        {"repository", "repositories", "host_aliases", "resolve_addressed_threads", "review_request_workflow", "gh_config_dir", "ssh_key"},
        "github",
    )
    gh_config_dir = (
        _nonblank_string(github["gh_config_dir"], "github.gh_config_dir")
        if "gh_config_dir" in github else ""
    )
    resolve_addressed_threads = github.get("resolve_addressed_threads", True)
    if not isinstance(resolve_addressed_threads, bool):
        raise EngineConfigError("github.resolve_addressed_threads must be a boolean")
    github_repository = _repository_slug(
        github.get("repository", ""), "github.repository"
    )

    repository_list = github.get("repositories", [])
    if not isinstance(repository_list, list):
        raise EngineConfigError("github.repositories must be a list of owner/name strings")
    github_repositories = tuple(
        _repository_slug(_nonblank_string(value, "github.repositories"), "github.repositories")
        for value in repository_list
    )

    access = _table(document.get("access", {}), "access")
    _reject_unknown(access, {"operators"}, "access")
    operators = _user_ids(access.get("operators", ()), "access.operators")

    server = _table(document.get("server", {}), "server")
    _reject_unknown(server, {"host", "port"}, "server")
    server_host = _nonblank_string(server.get("host", "localhost"), "server.host")
    server_port = server.get("port", 4364)
    if (
        not isinstance(server_port, int)
        or isinstance(server_port, bool)
        or not 0 <= server_port <= 65535
    ):
        raise EngineConfigError("server.port must be an integer from 0 to 65535")

    state = _table(document.get("state", {}), "state")
    _reject_unknown(state, {"directory", "sqlite_path", "graph_state_directory"}, "state")
    state_config = StateConfig(
        directory=_nonblank_string(state.get("directory", "."), "state.directory"),
        sqlite_path=_nonblank_string(
            state.get("sqlite_path", "conversations.sqlite3"), "state.sqlite_path"
        ),
        graph_state_directory=_nonblank_string(
            state.get("graph_state_directory", "graph-state"),
            "state.graph_state_directory",
        ),
    )

    communications = _table(document.get("communications", {}), "communications")
    _reject_unknown(communications, {"channel", "provider"}, "communications")
    communications_provider = _nonblank_string(
        communications.get("provider", "slack"), "communications.provider"
    )
    if communications_provider not in {"buzz", "slack"}:
        raise EngineConfigError(
            "communications.provider is unknown: "
            f"{communications_provider!r}; expected one of: buzz, slack"
        )
    communications_channel = _optional_nonblank_string(
        communications.get("channel", ""), "communications.channel"
    )

    work_orders = _table(document.get("work_orders", {}), "work_orders")
    _reject_unknown(
        work_orders, {"repository", "runner", "workflow", "slack_operators"}, "work_orders"
    )
    work_order_repository = _optional_nonblank_string(
        work_orders.get("repository", ""), "work_orders.repository"
    )
    work_order_workflow = _optional_nonblank_string(
        work_orders.get("workflow", ""), "work_orders.workflow"
    )
    work_order_runner = _optional_nonblank_string(
        work_orders.get("runner", ""), "work_orders.runner"
    )
    work_order_slack_operators = _strings(
        work_orders.get("slack_operators", ()), "work_orders.slack_operators"
    )
    if any(not user_id.strip() for user_id in work_order_slack_operators):
        raise EngineConfigError("work_orders.slack_operators must not contain empty user IDs")

    claude = _table(document.get("claude", {}), "claude")
    _reject_unknown(claude, {"output_style", "config_dir"}, "claude")
    config_dir = (
        _nonblank_string(claude["config_dir"], "claude.config_dir")
        if "config_dir" in claude else ""
    )
    output_style = _output_style(claude.get("output_style", ""))

    approvals = _table(document.get("approvals", {}), "approvals")
    _reject_unknown(approvals, {"auto_approve", "allow", "bash"}, "approvals")

    auto_approve = approvals.get("auto_approve", False)
    if not isinstance(auto_approve, bool):
        raise EngineConfigError("approvals.auto_approve must be a boolean")

    capability_names = _strings(approvals.get("allow", ("read",)), "approvals.allow")
    capabilities: list[ApprovalCapability] = []
    for name in capability_names:
        try:
            capabilities.append(ApprovalCapability(name))
        except ValueError as error:
            choices = ", ".join(capability.value for capability in ApprovalCapability)
            raise EngineConfigError(
                f"approvals.allow contains unknown capability {name!r}; expected one of: {choices}"
            ) from error

    bash = _table(approvals.get("bash", {}), "approvals.bash")
    _reject_unknown(bash, {"allow", "ask", "deny"}, "approvals.bash")

    graphs = _table(document.get("graphs", {}), "graphs")
    _reject_unknown(graphs, {"allow_python"}, "graphs")
    allow_python = graphs.get("allow_python", True)
    if not isinstance(allow_python, bool):
        raise EngineConfigError("graphs.allow_python must be a boolean")
    sessions = _table(document.get("sessions", {}), "sessions")
    _reject_unknown(sessions, {"tools"}, "sessions")
    session_tools = _strings(sessions.get("tools", DEFAULT_SESSION_TOOLS), "sessions.tools")
    for tool in session_tools:
        if tool not in REPOSITORY_TOOL_NAMES:
            raise EngineConfigError(
                f"sessions.tools contains unknown tool {tool!r}; expected one of: {', '.join(REPOSITORY_TOOL_NAMES)}"
            )
    model_tiers: dict[str, dict[str, str]] = {}
    runner_paths: dict[str, str] = {}
    for runner, settings in _table(document.get("runners", {}), "runners").items():
        settings = _table(settings, f"runners.{runner}")
        _reject_unknown(settings, {"models", "path"}, f"runners.{runner}")
        if "path" in settings:
            # A relative path would resolve against whatever directory the
            # agent starts in, which may be an untrusted checkout.
            path = Path(_nonblank_string(settings["path"], f"runners.{runner}.path")).expanduser()
            if not path.is_absolute():
                raise EngineConfigError(
                    f"runners.{runner}.path must be absolute or start with ~: {settings['path']!r}"
                )
            runner_paths[runner] = str(path)
        model_tiers[runner] = {
            tier: _optional_nonblank_string(model, f"runners.{runner}.models.{tier}")
            for tier, model in _table(settings.get("models", {}), f"runners.{runner}.models").items()
        }

    workflows = _table(document.get("workflows", {}), "workflows")
    _reject_unknown(workflows, {"directory"}, "workflows")
    workflow_directory = _optional_nonblank_string(
        workflows.get("directory", ""), "workflows.directory"
    )

    onboarding = _table(document.get("onboarding", {}), "onboarding")
    _reject_unknown(onboarding, {"directory"}, "onboarding")
    onboarding_directory = _optional_nonblank_string(
        onboarding.get("directory", ""), "onboarding.directory"
    )

    orchestrator = _table(document.get("orchestrator", {}), "orchestrator")
    _reject_unknown(
        orchestrator, {"host", "database", "health_check_interval"}, "orchestrator"
    )
    orchestrator_host = _nonblank_string(
        orchestrator.get("host", "127.0.0.1:7233"), "orchestrator.host"
    )
    orchestrator_database = _nonblank_string(
        orchestrator.get("database", ".engine/temporal.sqlite3"),
        "orchestrator.database",
    )
    health_check_interval = orchestrator.get("health_check_interval", 5.0)
    if (
        not isinstance(health_check_interval, (int, float))
        or isinstance(health_check_interval, bool)
        or health_check_interval <= 0
    ):
        raise EngineConfigError(
            "orchestrator.health_check_interval must be a positive number"
        )

    pull_requests = _table(document.get("pull_requests", {}), "pull_requests")
    _reject_unknown(pull_requests, {"auto_approve_green", "repos"}, "pull_requests")
    auto_approve_green = pull_requests.get("auto_approve_green", False)
    if not isinstance(auto_approve_green, bool):
        raise EngineConfigError("pull_requests.auto_approve_green must be a boolean")
    repo_configs, work_order_repository = _repo_configs(
        document, github, pull_requests, work_orders,
        (github_repository, *github_repositories), work_order_repository,
    )
    repos = {name: repo.path for name, repo in repo_configs.items()}
    repo_modes = {name: repo.mode for name, repo in repo_configs.items() if repo.mode is not None}
    repository_trust = {name: repo.trusted for name, repo in repo_configs.items() if repo.trusted is not None}
    trusted_repos = {name for name, repo in repo_configs.items() if repo.trusted}
    review_overrides = {name: repo.auto_approve_green for name, repo in repo_configs.items() if repo.auto_approve_green is not None}
    github_repositories = tuple(name for name, repo in repo_configs.items() if repo.webhooks)

    return EngineConfig(
        sandbox=SandboxConfig(backend=sandbox_backend, image=sandbox_image.strip()),
        attribution=attribution,
        repos=repos,
        repo_configs=repo_configs,
        pull_requests=PullRequestsConfig(auto_approve_green, review_overrides),
        repo_modes=repo_modes,
        trusted_repos=frozenset(trusted_repos),
        repository_trust=repository_trust,
        default_branch=default_branch,
        github_client_id=github_client_id,
        github_login_client_id=_optional_nonblank_string(
            document.get("github_login_client_id", ""), "github_login_client_id"
        ),
        github_login_redirect_uri=_optional_nonblank_string(
            document.get("github_login_redirect_uri", ""), "github_login_redirect_uri"
        ),
        github_token=github_token,
        public_url=public_url.rstrip("/"),
        server=ServerConfig(host=server_host, port=server_port),
        state=state_config,
        github=GitHubConfig(
            review_request_workflow=_nonblank_string(github.get("review_request_workflow", "review"), "github.review_request_workflow"),
            repository=github_repository,
            repositories=github_repositories,
            resolve_addressed_threads=resolve_addressed_threads,
            gh_config_dir=gh_config_dir,
            ssh_key=_nonblank_string(github["ssh_key"], "github.ssh_key") if "ssh_key" in github else "",
            host_aliases={
                _nonblank_string(alias, "github.host_aliases").lower():
                _nonblank_string(target, "github.host_aliases").lower()
                for alias, target in _table(
                    github.get("host_aliases", {}), "github.host_aliases"
                ).items()
            },
        ),
        access=AccessConfig(operators=operators),
        communications=CommunicationsConfig(
            provider=communications_provider,
            channel=communications_channel,
        ),
        work_orders=WorkOrdersConfig(
            repository=work_order_repository,
            workflow=work_order_workflow,
            runner=work_order_runner,
            slack_operators=work_order_slack_operators,
        ),
        claude=ClaudeConfig(output_style=output_style, config_dir=config_dir),
        graphs=GraphsConfig(allow_python=allow_python),
        sessions=SessionsConfig(tools=tuple(session_tools)),
        model_tiers=model_tiers,
        runner_paths=runner_paths,
        approvals=ApprovalConfig(
            auto_approve=auto_approve,
            allow=tuple(capabilities),
            bash=BashApprovalConfig(
                allow=_patterns(bash.get("allow", ()), "approvals.bash.allow"),
                ask=_patterns(bash.get("ask", ()), "approvals.bash.ask"),
                deny=_patterns(bash.get("deny", ()), "approvals.bash.deny"),
            ),
        ),
        workflows=WorkflowsConfig(directory=workflow_directory),
        orchestrator=OrchestratorConfig(
            host=orchestrator_host,
            database=orchestrator_database,
            health_check_interval=float(health_check_interval),
        ),
        onboarding=OnboardingConfig(directory=onboarding_directory),
    )


def _optional_nonblank_string(value: object, name: str) -> str:
    """Validate a string setting which may be omitted but never whitespace."""

    if not isinstance(value, str):
        raise EngineConfigError(f"{name} must be a string")
    if value and not value.strip():
        raise EngineConfigError(f"{name} must not be blank")
    return value.strip()


def _repo_configs(
    document: Mapping[str, object], github: Mapping[str, object],
    pull_requests: Mapping[str, object], work_orders: Mapping[str, object],
    webhook_names: tuple[str, ...], work_order_repository: str,
) -> tuple[dict[str, RepoConfig], str]:
    entries: dict[str, dict[str, object]] = {}
    for name, value in _table(document.get("repos", {}), "repos").items():
        name = _nonblank_string(name, "repos name")
        entry: dict[str, object] = {"path": value} if isinstance(value, str) else dict(_table(value, f"repos.{name}"))
        _reject_unknown(entry, {"path", "mode", "trusted", "auto_approve_green", "webhooks", "default"}, f"repos.{name}")
        if "mode" in entry and entry["mode"] != "disconnected":
            raise EngineConfigError(f'repos.{name}.mode must be "disconnected"; omit mode for connected repositories')
        entries[name] = entry

    def warn(section: str, spelling: str) -> None:
        logging.getLogger(__name__).warning(
            "%s is deprecated and will be removed in the next release; use %s", section, spelling,
        )

    def merge(name: str, key: str, value: object, section: str) -> None:
        if name not in entries:
            raise EngineConfigError(f"{section}.{name} names no repository under [repos]")
        entry = entries[name]
        if key in entry and (entry[key] != value or type(entry[key]) is not type(value)):
            raise EngineConfigError(f"{section} conflicts with repos.{name}.{key}")
        entry[key] = value

    for section, key, source in (
        ("repo_modes", "mode", document),
        ("trusted_repos", "trusted", document),
        ("repos", "auto_approve_green", pull_requests),
    ):
        label = "pull_requests.repos" if source is pull_requests else section
        if section in source:
            warn(label, f'[repos."owner/name"] {key} = ...')
            for name, value in _table(source[section], label).items():
                merge(name, key, value, label)

    for key in ("repository", "repositories"):
        if key in github:
            warn(f"github.{key}", '[repos."owner/name"] webhooks = true (the default)')
    names = {name.lower(): name for name in entries}
    for repository in webhook_names:
        if not repository:
            continue
        if repository.lower() not in names:
            raise EngineConfigError(
                f"GitHub webhook repository {repository!r} requires a checkout path under [repos]"
            )
        merge(names[repository.lower()], "webhooks", True, "github")

    configs = {}
    for name, entry in entries.items():
        path = _nonblank_string(entry.get("path"), f"repos.{name}.path")
        mode = entry.get("mode")
        if "mode" in entry and (not isinstance(mode, str) or mode not in REPO_MODES):
            raise EngineConfigError(f"repos.{name}.mode must be one of: {', '.join(REPO_MODES)}")
        flags: dict[str, bool] = {}
        for key in ("trusted", "auto_approve_green", "webhooks", "default"):
            if key in entry:
                value = entry[key]
                if not isinstance(value, bool):
                    raise EngineConfigError(f"repos.{name}.{key} must be a boolean")
                flags[key] = value
        try:
            _repository_slug(name, "repos name")
            slug = True
        except EngineConfigError:
            slug = False
        configs[name] = RepoConfig(
            path=path, mode=mode if isinstance(mode, str) else None, trusted=flags.get("trusted"),
            auto_approve_green=flags.get("auto_approve_green"),
            webhooks=slug and flags.get("webhooks", True), default=flags.get("default", False),
        )

    defaults = [name for name, repo in configs.items() if repo.default]
    if len(defaults) > 1:
        raise EngineConfigError("at most one repository may set default = true")
    if "repository" in work_orders:
        warn("work_orders.repository", '[repos."owner/name"] default = true')
        matching = work_order_repository if work_order_repository in configs else next(
            (name for name, repo in configs.items()
             if work_order_repository and Path(repo.path).expanduser().resolve()
             == Path(work_order_repository).expanduser().resolve()),
            None,
        )
        if matching is not None:
            merge(matching, "default", True, "work_orders.repository")
            if defaults and defaults != [matching]:
                raise EngineConfigError("work_orders.repository conflicts with repository default = true")
            configs[matching] = replace(configs[matching], default=True)
            defaults = [matching]
        elif defaults and work_order_repository:
            raise EngineConfigError("work_orders.repository conflicts with repository default = true")
    if defaults:
        work_order_repository = configs[defaults[0]].path
    return configs, work_order_repository


def _repository_slug(value: object, name: str) -> str:
    """A repository is named the way GitHub names it, or not accepted at all.

    A misspelled slug would not fail loudly: every delivery would simply be
    answered as if it came from somewhere else, which is the silence a strict
    configuration file exists to prevent.
    """

    slug = _optional_nonblank_string(value, name)
    if not slug:
        return ""
    owner, separator, repository = slug.partition("/")
    if not separator or not owner or not repository or "/" in repository:
        raise EngineConfigError(f'{name} must be "owner/name": {slug!r}')
    if any(character.isspace() for character in slug):
        raise EngineConfigError(f'{name} must be "owner/name": {slug!r}')
    return slug


def _nonblank_string(value: object, name: str) -> str:
    value = _optional_nonblank_string(value, name)
    if not value:
        raise EngineConfigError(f"{name} must not be blank")
    return value


def describe_loaded_config(loaded: LoadedEngineConfig) -> str:
    """A compact startup description of the policy this process will apply."""

    source = str(loaded.path) if loaded.path is not None else "defaults (no engine.toml)"
    approvals = loaded.config.approvals
    capabilities = ", ".join(capability.value for capability in approvals.allow) or "none"
    bash_rules = sum(
        len(patterns)
        for patterns in (approvals.bash.allow, approvals.bash.ask, approvals.bash.deny)
    )
    auto_approve = "on" if approvals.auto_approve else "off"
    workflows = (
        str(loaded.workflows_directory)
        if loaded.workflows_directory is not None
        else "disabled"
    )
    attribution = "on" if loaded.config.attribution else "off"
    default_branch = loaded.config.default_branch
    style = loaded.config.claude.output_style
    output_style = style.value if style is not None else "provider default"
    return (
        f"configuration: {source}; attribution={attribution}; default_branch={default_branch}; "
        f"claude.output_style={output_style}; "
        f"claude.config_dir={loaded.claude_config_dir or 'provider default'}; "
        f"github.gh_config_dir={loaded.gh_config_dir or 'provider default'}; approvals enforced "
        f"(auto_approve={auto_approve}, allow={capabilities}, bash_rules={bash_rules}); "
        f"workflows={workflows}"
    )


def _output_style(value: object) -> ResponseStyle | None:
    """Validated here rather than passed through, because a provider that does
    not recognize a style name may ignore it instead of refusing it -- and a
    misspelled style that quietly does nothing is the one failure a strict
    configuration file exists to prevent."""
    if not isinstance(value, str):
        raise EngineConfigError("claude.output_style must be a string")
    if not value:
        return None
    try:
        return ResponseStyle(value)
    except ValueError as error:
        choices = ", ".join(style.value for style in ResponseStyle)
        raise EngineConfigError(
            f"claude.output_style is unknown: {value!r}; expected one of: {choices}"
        ) from error


def _relative_to(path: Path, directory: Path) -> Path:
    return path if path.is_absolute() else directory / path


def _table(value: object, location: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise EngineConfigError(f"{location} must be a TOML table")
    return value


def _strings(value: object, location: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise EngineConfigError(f"{location} must be an array of strings")
    strings = tuple(value)
    if not all(isinstance(item, str) for item in strings):
        raise EngineConfigError(f"{location} must be an array of strings")
    if len(set(strings)) != len(strings):
        raise EngineConfigError(f"{location} must not contain duplicates")
    return strings


def _user_ids(value: object, location: str) -> tuple[int, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise EngineConfigError(f"{location} must be an array of GitHub user IDs")
    ids = tuple(value)
    if not all(type(item) is int and item > 0 for item in ids):
        raise EngineConfigError(
            f"{location} must contain numeric GitHub user IDs, not logins"
        )
    if len(set(ids)) != len(ids):
        raise EngineConfigError(f"{location} must not contain duplicates")
    return ids


def _patterns(value: object, location: str) -> tuple[str, ...]:
    patterns = _strings(value, location)
    if any(not pattern.strip() for pattern in patterns):
        raise EngineConfigError(f"{location} must not contain empty patterns")
    return patterns


def _reject_unknown(
    values: Mapping[str, object], allowed: set[str], location: str
) -> None:
    if unknown := sorted(set(values) - allowed):
        raise EngineConfigError(f"unknown key in {location}: {unknown[0]}")


__all__ = [
    "AccessConfig",
    "ApprovalCapability",
    "ApprovalConfig",
    "BashApprovalConfig",
    "CONFIG_ENVIRONMENT_VARIABLE",
    "ClaudeConfig",
    "DEFAULT_CONFIG_NAME",
    "DEFAULT_CONFIG_TEMPLATE",
    "DEFAULT_MODEL_TIERS",
    "EngineConfig",
    "EngineConfigError",
    "GitHubConfig",
    "PullRequestsConfig",
    "GraphsConfig",
    "SessionsConfig",
    "DEFAULT_SESSION_TOOLS",
    "LoadedEngineConfig",
    "ResponseStyle",
    "ServerConfig",
    "SandboxConfig",
    "StateConfig",
    "WorkOrdersConfig",
    "WorkflowsConfig",
    "describe_loaded_config",
    "load_engine_config",
    "merge_model_tiers",
    "parse_engine_config",
]
