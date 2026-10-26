"""Live repository configuration. Updates publish one immutable snapshot."""
from __future__ import annotations

import os
import subprocess
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from types import MappingProxyType

from engine.runtime.change_requests import remote_project
from engine.runtime.config import EngineConfigError, parse_engine_config


def normalize_repository(
    repository: str, repos: Mapping[str, str] | None = None, *, configured: bool = False,
) -> str:
    """Resolve configured local directories on the server; preserve unregistered forge slugs."""
    repos = repos or {}
    if repository in repos:
        repository = repos[repository]
        configured = True
    else:
        folded = repository.lower().removesuffix(".git")
        for name, path in repos.items():
            if name.lower() == folded or repository == path:
                repository = path
                configured = True
                break
    expanded = os.path.expanduser(repository)
    return os.path.abspath(expanded) if configured and expanded else expanded


@dataclass(frozen=True)
class RepositorySnapshot:
    repos: Mapping[str, str]
    trust_decisions: Mapping[str, bool]
    repo_modes: Mapping[str, str]
    trusted_repos: frozenset[str]
    projects: Mapping[str, str]
    disconnected: frozenset[Path]
    trusted: frozenset[Path]
    login_repositories: tuple[str, ...]
    run_projects: Mapping[str, str]


class RepositoryRegistry:
    """Own configured checkouts and their access policy; never write configuration.

    Adding a GitHub checkout immediately lets its writers sign in. Existing
    names cannot be replaced. Readers may retain a snapshot across async work.
    """

    def __init__(
        self,
        repos: Mapping[str, str] | None = None,
        repo_modes: Mapping[str, str] | None = None,
        trusted_repos: Collection[str] | Mapping[str, bool] = (),
        *,
        host_aliases: Collection[str] = (),
        projects: Mapping[str, str] | None = None,
        login_repositories: Collection[str] = (),
    ) -> None:
        self._lock = RLock()
        self._hosts = frozenset(host_aliases)
        self._extra_login = tuple(login_repositories)
        entries: dict[str, dict[str, object]] = {name: {"path": path} for name, path in (repos or {}).items()}
        trust = dict(trusted_repos) if isinstance(trusted_repos, Mapping) else dict.fromkeys(trusted_repos, True)
        for key, values in (("mode", repo_modes or {}), ("trusted", trust)):
            for name, value in values.items():
                if name not in entries:
                    message = f"{name} names no repository under [repos]"
                    raise EngineConfigError(message)
                if key != "mode" or value != "connected":
                    entries[name][key] = value
        config = parse_engine_config({"repos": entries})
        resolved = dict(projects) if projects is not None else {
            name: project for name, path in {".": ".", **config.repos}.items()
            if (project := self._project(path)) is not None
        }
        self._snapshot = self._build(config.repos, config.repo_modes, config.repository_trust, resolved)

    def _project(self, path: str) -> str | None:
        try:
            remote = subprocess.run(
                ["git", "-C", str(Path(path).expanduser()), "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=10, check=True,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        project = remote_project(remote)
        if project is not None:
            host, _, rest = project.partition("/")
            if "/" not in rest or host in self._hosts:
                return project
        return None

    def _build(
        self, repos: Mapping[str, str], modes: Mapping[str, str],
        trusted: Mapping[str, bool], projects: Mapping[str, str],
    ) -> RepositorySnapshot:
        paths = {name: Path(path).expanduser().resolve() for name, path in repos.items()}
        run_projects = {}
        for name, project in projects.items():
            path = str(paths.get(name, Path(name).expanduser().resolve()))
            run_projects[name] = run_projects[path] = project.lower()
        return RepositorySnapshot(
            MappingProxyType(dict(repos)), MappingProxyType(dict(trusted)), MappingProxyType(dict(modes)),
            frozenset(name for name, value in trusted.items() if value),
            MappingProxyType(dict(projects)),
            frozenset(paths[name] for name in repos if modes.get(name) == "disconnected"),
            frozenset(paths[name] for name, value in trusted.items() if value),
            tuple(dict.fromkeys((*self._extra_login, *(projects[n] for n in repos if n in projects)))),
            MappingProxyType(run_projects),
        )

    @property
    def snapshot(self) -> RepositorySnapshot:
        with self._lock:
            return self._snapshot

    def add(self, name: str, path: str, mode: str | None = None, *, trusted: bool | None = None) -> None:
        """Validate like config loading, resolve origin once, then publish atomically."""
        entry: dict[str, object] = {"path": path}
        if mode is not None and mode != "connected":
            entry["mode"] = mode
        if trusted is not None:
            entry["trusted"] = trusted
        config = parse_engine_config({"repos": {name: entry}})
        if name in self.snapshot.repos:
            message = f"repos.{name} already exists"
            raise EngineConfigError(message)
        # Git may block; readers must remain free to acquire the snapshot lock.
        project = self._project(path)
        with self._lock:
            previous = self._snapshot
            if name in previous.repos:
                message = f"repos.{name} already exists"
                raise EngineConfigError(message)
            projects = dict(previous.projects)
            if project is not None:
                projects[name] = project
            self._snapshot = self._build(
                {**previous.repos, **config.repos}, {**previous.repo_modes, **config.repo_modes},
                {**previous.trust_decisions, **config.repository_trust}, projects,
            )

    def trust(self, name: str, trusted: bool) -> None:
        """Publish a trust decision without changing the registered checkout."""
        with self._lock:
            previous = self._snapshot
            if name not in previous.repos:
                message = f"{name} names no repository under [repos]"
                raise EngineConfigError(message)
            config = parse_engine_config({"repos": {name: {"path": previous.repos[name], "trusted": trusted}}})
            self._snapshot = self._build(previous.repos, previous.repo_modes,
                {**previous.trust_decisions, **config.repository_trust}, previous.projects)

    def lookup(self, checkout: str) -> tuple[str | None, bool | None]:
        snapshot = self.snapshot
        if checkout in snapshot.repos:
            return checkout, snapshot.trust_decisions.get(checkout)
        for name, project in snapshot.projects.items():
            if name in snapshot.repos and project.lower() == checkout.lower().removesuffix(".git"):
                return name, snapshot.trust_decisions.get(name)
        identity = repository_identity(Path(checkout))
        for name, path in snapshot.repos.items():
            if repository_identity(Path(path)) == identity:
                return name, snapshot.trust_decisions.get(name)
        return None, None


def repository_identity(path: Path) -> Path:
    """Compare subdirectories and worktrees by their common git directory."""
    path = path.expanduser().resolve()
    try:
        result = subprocess.run(["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, timeout=10, check=True)
        return Path(result.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        return path
