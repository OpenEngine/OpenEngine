"""Daemon-owned repository onboarding, shared by all clients."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock
from uuid import uuid4

import tomlkit
from tomlkit.items import InlineTable
from filelock import FileLock
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from engine.cli import starters
from engine.graph_service import GraphService

from engine.apps.web.repositories import ensure_repository_checkouts
from engine.runtime.config import parse_engine_config
from engine.runtime.repositories import RepositoryRegistry


class OnboardingError(ValueError):
    def __init__(self, code: str, message: str, fix: str) -> None:
        super().__init__(message)
        self.problem = {"code": code, "message": message, "fix": fix}


def repository_name(reference: str) -> str:
    reference = reference.strip().rstrip("/")
    match = re.fullmatch(
        r"(?:(?:https://|ssh://git@)(github\.com|gitlab\.com)/|git@(github\.com|gitlab\.com):)?"
        r"([A-Za-z0-9_][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_][A-Za-z0-9_.-]*)+)", reference,
    )
    if match:
        host = match[1] or match[2] or "github.com"
        name = match[3].removesuffix(".git")
        if all(part not in {".", ".."} for part in name.split("/")) and (host == "gitlab.com" or name.count("/") == 1):
            return name if host == "github.com" else f"{host}/{name}"
    raise OnboardingError("repository", "Invalid GitHub/GitLab repository reference", "Use https://github.com/owner/repo or https://gitlab.com/group/repo")


def local_repository(checkout: Path, repos: Mapping[str, str]) -> tuple[str, str | None]:
    """Name a checkout by its forge origin or a stable, slash-free local alias."""
    checkout = checkout.expanduser().resolve()
    origin = _run(["git", "-C", str(checkout), "remote", "get-url", "origin"])
    if origin.returncode == 0 and re.match(r"(?:https://|ssh://git@|git@)(?:github\.com|gitlab\.com)[/:]", origin.stdout.strip()):
        try:
            project = repository_name(origin.stdout.strip())
        except OnboardingError:
            pass
        else:
            return project, project
    for name, path in repos.items():
        if Path(path).expanduser().resolve() == checkout:
            return name, None
    # Config accepts nonblank strings; use a portable TOML bare key without
    # slashes so local aliases cannot overlap owner/repo forge identities.
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", checkout.name).strip("-") or "repo"
    if name in repos:
        raise repository_name_collision(name, str(checkout), repos[name])
    return name, None


def repository_name_collision(name: str, checkout: str, existing: str) -> OnboardingError:
    fix = "Create a unique alias for this checkout in [repos] in engine.toml, or rename the local checkout directory and retry"
    return OnboardingError(
        "config", f"Repository name {name!r} for {checkout} conflicts with {existing}. {fix}", fix,
    )


def machine_config_path() -> Path:
    home = Path(os.environ.get("XDG_CONFIG_HOME", ""))
    if not home.is_absolute():
        home = Path.home() / ".config"
    return home / "openengine" / "engine.toml"


def _run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, capture_output=True, text=True, timeout=20, check=False)


def agent_available(kind: str, *, model: str = "", url: str = "") -> bool:
    """Whether this backend can launch a signed-in harness, without starting a session."""
    if not shutil.which(kind):
        return False
    if kind in ("claude", "codex"):
        command = ["claude", "auth", "status", "--json"] if kind == "claude" else ["codex", "login", "status"]
        try:
            result = _run(command)
            return result.returncode == 0 and (
                kind == "codex" or json.loads(result.stdout).get("loggedIn") is True
            )
        except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
            return False
    if kind == "opencode":
        # An added agent with its own endpoint needs no provider sign-in.
        if url:
            return True
        data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
        try:
            credentials = json.loads((data_home / "opencode/auth.json").read_text())
            if not isinstance(credentials, dict):
                return False
            provider = model.split("/", 1)[0] if "/" in model else ""
            return any(
                (not provider or name == provider) and isinstance(item, dict) and (
                    (item.get("type") == "api" and bool(item.get("key")))
                    or (item.get("type") == "oauth" and bool(item.get("refresh")))
                ) for name, item in credentials.items()
            )
        except (OSError, ValueError):
            return False
    return False


def signed_in_agent() -> str | None:
    return next((kind for kind in ("claude", "codex", "opencode") if agent_available(kind)), None)


def preflight() -> tuple[list[dict[str, str]], str | None]:
    problems = []
    if not shutil.which("git"):
        problems.append({"code": "git", "message": "git is not installed", "fix": "Install git: brew install git (macOS) or sudo apt install git (Debian/Ubuntu)"})
    agent = signed_in_agent()
    if agent is None:
        problems.append({"code": "agent", "message": "No agent is signed in on the daemon account", "fix": "Run oe agent signin claude, oe agent signin codex, or oe agent signin opencode as the daemon account"})
    return problems, agent


def validate_checkout(name: str, path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    try:
        root = _run(["git", "-C", str(path), "rev-parse", "--show-toplevel"])
        origin = _run(["git", "-C", str(path), "remote", "get-url", "origin"])
        if root.returncode == 0 and Path(root.stdout.strip()).resolve() == path.resolve():
            if "/" not in name or (origin.returncode == 0 and repository_name(origin.stdout.strip()).lower() == name.lower()):
                return
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    raise OnboardingError("destination", f"{path} is not a checkout of {name}", "Choose an unused --path or the existing checkout of this repository")


def _updated_config(path: Path, name: str, checkout: str, mode: str, trusted: bool | None = None) -> tuple[str, str]:
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    document = tomlkit.parse(original)
    repos = document.setdefault("repos", tomlkit.table())
    if isinstance(repos, InlineTable):
        expanded = tomlkit.table()
        for key, value in repos.items():
            expanded[key] = value
        expanded.comment(repos.trivia.comment)
        document["repos"] = repos = expanded
    if name in repos:
        existing = repos[name]
        existing_path = existing["path"] if isinstance(existing, dict) else str(existing)
        if Path(existing_path).expanduser().resolve() != Path(checkout).expanduser().resolve():
            raise repository_name_collision(name, checkout, existing_path)
        if isinstance(existing, str):
            # Migrate only the entry being updated, preserving unrelated legacy entries.
            entry = tomlkit.table()
            entry["path"] = existing
            for section, key in (("repo_modes", "mode"), ("trusted_repos", "trusted")):
                if name in document.get(section, {}):
                    entry[key] = document[section].pop(name)
            repos[name] = entry
    else:
        entry = tomlkit.table()
        entry["path"] = checkout
        if mode == "disconnected":
            entry["mode"] = mode
        repos[name] = entry
    if trusted is not None:
        repos[name]["trusted"] = trusted
        if name in document.get("trusted_repos", {}):
            del document["trusted_repos"][name]
    parse_engine_config(document.unwrap())
    return original, tomlkit.dumps(document)


def persist_repository(path: Path, name: str, checkout: str, mode: str, trusted: bool | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + ".lock"):
        original, updated = _updated_config(path, name, checkout, mode, trusted)
        if updated == original:
            return
        temporary = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(updated)
                stream.flush()
                os.fsync(stream.fileno())
            if path.exists():
                temporary.chmod(path.stat().st_mode & 0o777)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


async def register_starters(service: GraphService | None, project: str, runner: str) -> None:
    if service is None:
        raise OnboardingError("graphs", "The graph service is unavailable", "Check daemon health and graph configuration, then retry")
    for name in starters.NAMES:
        source = starters.source(name, runner="" if name == starters.DEFAULT else runner)
        await service.add_graph(project, source=source, format="python")


class RepositoryOnboarding:
    def __init__(self, registry: RepositoryRegistry, config_path: Path | None,
                 register_starters: Callable[[str, str], Awaitable[None]],
                 onboarding_directory: Path | None = None) -> None:
        self.registry = registry
        self.config_path = config_path or machine_config_path()
        self.register_starters = register_starters
        self.onboarding_directory = onboarding_directory
        self.jobs: dict[str, dict] = {}
        self.tasks: set[asyncio.Task] = set()
        self.lock = asyncio.Lock()
        self.config_lock = RLock()

    def _default_checkout(self, name: str) -> Path:
        directory = self.onboarding_directory if self.onboarding_directory is not None else Path.home() / "code"
        return (directory / name.split("/")[-1]).expanduser().resolve()

    def _reserved_repositories(self) -> dict[str, str]:
        return {**self.registry.snapshot.repos, **{
            job["repository"]: job["path"] for job in self.jobs.values()
            if job["status"] != "failed"
        }}

    def start(self, reference: str, path: str | None, mode: str | None = None, trusted: bool | None = None) -> dict:
        local_only = False
        local = Path(reference).expanduser()
        if local.exists() or reference.startswith((".", "/", "~")):
            root = _run(["git", "-C", str(local), "rev-parse", "--show-toplevel"])
            if root.returncode:
                raise OnboardingError("repository", "Not a local checkout", "Choose an existing git checkout")
            checkout = Path(root.stdout.strip()).resolve()
            if path and Path(path).expanduser().resolve() != checkout:
                raise OnboardingError("path", "--path is only for cloning", "Omit --path when adding an existing checkout")
            path = str(checkout)
            reference = path
            name, project = local_repository(checkout, self._reserved_repositories())
            local_only = project is None
            if local_only and mode == "connected":
                raise OnboardingError("mode", "Connected mode requires a GitHub/GitLab origin",
                                      "Add a GitHub/GitLab origin, or use disconnected mode")
        else:
            name = repository_name(reference)
        if mode is None:
            mode = "disconnected" if local_only else "connected"
        if mode not in {"connected", "disconnected"}:
            raise OnboardingError("mode", "Invalid mode", "Choose connected or disconnected")
        for job in self.jobs.values():
            if job["repository"] == name and job["status"] not in {"failed", "complete"}:
                return dict(job)
        existing = self.registry.snapshot.repos.get(name)
        checkout = Path(path or existing or self._default_checkout(name)).expanduser().resolve()
        if existing and Path(existing).expanduser().resolve() != checkout:
            raise OnboardingError("destination", f"{name} is already configured at {existing}", "Omit --path to use the configured checkout")
        for previous in self.jobs.values():
            if previous["repository"] == name and previous["status"] == "complete" and previous["path"] == str(checkout) and checkout.is_dir() and trusted is None:
                return {**previous, "message": "Already onboarded"}
        job = {"id": uuid4().hex, "repository": name, "path": str(checkout), "mode": mode, "trusted": trusted,
               "local_only": local_only,
               "clone_url": reference if ":" in reference or Path(reference).is_absolute() else f"git@github.com:{name}.git",
               "status": "queued", "message": "Waiting to onboard", "problems": []}
        self.jobs[job["id"]] = job
        task = asyncio.create_task(self._onboard(job))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return dict(job)

    async def _onboard(self, job: dict) -> None:
        async with self.lock:
            try:
                name, path, mode = job["repository"], Path(job["path"]), job["mode"]
                job.update(status="preflight", message="Checking git and agent sign-in")
                already_configured = name in self.registry.snapshot.repos
                problems, agent = await asyncio.to_thread(preflight)
                if problems:
                    job.update(status="failed", message="Preflight failed", problems=problems)
                    return
                await asyncio.to_thread(validate_checkout, name, path)
                _, configured = await asyncio.to_thread(_updated_config, self.config_path, name, str(path), mode, job.get("trusted"))
                mode = parse_engine_config(tomlkit.parse(configured).unwrap()).repo_modes.get(name, "connected")
                job["mode"] = mode
                job.update(status="cloning", message=f"Preparing checkout of {name}")
                if not job["local_only"]:
                    await asyncio.to_thread(ensure_repository_checkouts, {name: str(path)}, clone_urls={name: job["clone_url"]})
                await asyncio.to_thread(validate_checkout, name, path)
                job.update(status="registering", message="Registering starter graphs")
                await self.register_starters(name, agent or "claude")
                job.update(status="configuring", message="Saving repository configuration")
                await asyncio.to_thread(self._persist, name, str(path), mode, job.get("trusted"))
                job.update(status="complete", message="Already onboarded" if already_configured else "Repository onboarded")
            except OnboardingError as error:
                job.update(status="failed", message=str(error), problems=[error.problem])
            except Exception as error:  # noqa: BLE001 -- retain background failures for polling clients
                # Jobs must expose failures after the initiating HTTP request has ended.
                job.update(status="failed", message="Onboarding failed", problems=[{
                    "code": "onboarding", "message": str(error),
                    "fix": "Check the daemon's configuration, graph service, SSH access and destination permissions, then retry",
                }])

    def _persist(self, name: str, checkout: str, mode: str, trusted: bool | None) -> None:
        # Keep publication in the same order as durable writes, including when
        # onboarding and trust requests arrive concurrently.
        with self.config_lock:
            persist_repository(self.config_path, name, checkout, mode, trusted)
            if name not in self.registry.snapshot.repos:
                self.registry.add(name, checkout, mode if mode == "disconnected" else None, trusted=trusted)
            elif trusted is not None:
                self.registry.trust(name, trusted)

    def repository_trust(self, repository: str, trusted: bool | None = None) -> dict:
        name, decision = self.registry.lookup(repository)
        if name is not None:
            checkout = self.registry.snapshot.repos[name]
        else:
            root = _run(["git", "-C", str(Path(repository).expanduser()), "rev-parse", "--show-toplevel"])
            if root.returncode:
                raise OnboardingError("repository", "Repository is not a configured name or local checkout", "Use a checkout path or onboard owner/repo first")
            checkout = str(Path(root.stdout.strip()).resolve())
            name, _ = local_repository(Path(checkout), self._reserved_repositories())
            validate_checkout(name, Path(checkout))
        if trusted is not None:
            _, project = local_repository(Path(checkout), self.registry.snapshot.repos)
            self._persist(name, checkout, "connected" if project else "disconnected", trusted)
            decision = trusted
        return {"repository": checkout, "name": name, "trusted": decision}

    def start_request(self, body: object) -> dict:
        if not isinstance(body, dict) or not isinstance(body.get("repository"), str):
            raise OnboardingError("repository", "Invalid repository", "Provide a checkout path or GitHub/GitLab URL")
        if body.get("path") is not None and (not isinstance(body["path"], str) or not body["path"].strip()):
            raise OnboardingError("path", "Invalid checkout path", "Provide a non-empty path on the daemon machine")
        mode = body.get("mode")
        if mode is not None and not isinstance(mode, str):
            raise OnboardingError("mode", "Invalid mode", "Choose connected or disconnected")
        trusted = body.get("trusted")
        if trusted is not None and not isinstance(trusted, bool):
            raise OnboardingError("trusted", "Invalid trust decision", "Provide a boolean")
        return self.start(body["repository"], body.get("path"), mode, trusted)

    def routes(self, visible_repositories: Callable[[Request], Awaitable[object]],
               is_local_request: Callable[[Request], bool]) -> list[Route]:
        async def start(request: Request) -> JSONResponse:
            if not is_local_request(request):
                return JSONResponse({"error": "forbidden"}, status_code=403)
            if await visible_repositories(request) is not None:
                return JSONResponse({"error": "repository onboarding is for operators"}, status_code=403)
            try:
                body = await request.json()
                return JSONResponse(self.start_request(body), status_code=202)
            except OnboardingError as error:
                return JSONResponse({"status": "failed", "message": str(error), "problems": [error.problem]})
            except ValueError:
                return JSONResponse({"error": "invalid JSON request"}, status_code=400)

        async def status(request: Request) -> JSONResponse:
            if await visible_repositories(request) is not None:
                return JSONResponse({"error": "repository onboarding is for operators"}, status_code=403)
            job = self.jobs.get(request.path_params["job_id"])
            if job is None:
                return JSONResponse({"error": "onboarding job not found; retry onboarding"}, status_code=404)
            return JSONResponse(dict(job))

        return [Route("/api/repositories/onboard", start, methods=["POST"]),
                Route("/api/repositories/onboard/{job_id}", status)]

    async def close(self) -> None:
        # Do not cancel a to_thread clone/config write while it is still running.
        if self.tasks:
            await asyncio.gather(*self.tasks)
