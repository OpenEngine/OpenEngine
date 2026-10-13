"""Daemon-owned repository onboarding, shared by all clients."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import uuid4

import tomlkit
from filelock import FileLock
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from engine.cli import starters
from engine.graph_service import GraphService

from engine.adapters.source_control.github import _parse_repo_coords
from engine.apps.web.repositories import ensure_repository_checkouts
from engine.runtime.config import parse_engine_config
from engine.runtime.repositories import RepositoryRegistry


class OnboardingError(ValueError):
    def __init__(self, code: str, message: str, fix: str) -> None:
        super().__init__(message)
        self.problem = {"code": code, "message": message, "fix": fix}


def repository_name(reference: str) -> str:
    # The adapter also accepts other hosts and trailing URL components. Onboarding
    # deliberately supports only complete GitHub repository references.
    reference = re.sub(r"^gh\s+repo\s+clone\s+", "", reference.strip())
    if not re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:)?"
        r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*", reference,
    ):
        raise OnboardingError("repository", "Invalid GitHub repository reference", "Use https://github.com/owner/repo")
    return "/".join(_parse_repo_coords(reference))


def machine_config_path() -> Path:
    home = Path(os.environ.get("XDG_CONFIG_HOME", ""))
    if not home.is_absolute():
        home = Path.home() / ".config"
    return home / "openengine" / "engine.toml"


def _run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, capture_output=True, text=True, timeout=20, check=False)


def signed_in_agent() -> str | None:
    for agent, command in (("claude", ["claude", "auth", "status", "--json"]),
                           ("codex", ["codex", "login", "status"])):
        if not shutil.which(agent):
            continue
        try:
            result = _run(command)
            if result.returncode == 0:
                if agent == "codex" or json.loads(result.stdout).get("loggedIn") is True:
                    return agent
        except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
            continue
    if shutil.which("opencode"):
        data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
        try:
            credentials = json.loads((data_home / "opencode/auth.json").read_text())
            if isinstance(credentials, dict) and any(
                isinstance(item, dict) and (
                    (item.get("type") == "api" and item.get("key"))
                    or (item.get("type") == "oauth" and item.get("refresh"))
                ) for item in credentials.values()
            ):
                return "opencode"
        except (OSError, ValueError):
            pass
    return None


def preflight() -> tuple[list[dict[str, str]], str | None]:
    problems = []
    if not shutil.which("git"):
        problems.append({"code": "git", "message": "git is not installed", "fix": "Install git: brew install git (macOS) or sudo apt install git (Debian/Ubuntu)"})
    try:
        result = _run(["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                       "-o", "ConnectTimeout=10", "-T", "git@github.com"])
        # GitHub returns 1 even after a successful SSH authentication.
        accessible = result.returncode == 1 and "successfully authenticated" in result.stdout + result.stderr
    except (OSError, subprocess.SubprocessError):
        accessible = False
    if not accessible:
        problems.append({"code": "github_ssh", "message": "GitHub SSH access is unavailable to the daemon account", "fix": "Run ssh -T git@github.com as the daemon account; configure its SSH key in GitHub and verify the host key, then retry"})
    agent = signed_in_agent()
    if agent is None:
        problems.append({"code": "agent", "message": "No agent is signed in on the daemon account", "fix": "Run engine agent signin claude, engine agent signin codex, or engine agent signin opencode as the daemon account"})
    return problems, agent


def validate_checkout(name: str, path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    try:
        root = _run(["git", "-C", str(path), "rev-parse", "--show-toplevel"])
        origin = _run(["git", "-C", str(path), "remote", "get-url", "origin"])
        if root.returncode == origin.returncode == 0 and Path(root.stdout.strip()).resolve() == path.resolve():
            if repository_name(origin.stdout.strip()).lower() == name.lower():
                return
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    raise OnboardingError("destination", f"{path} is not a checkout of {name}", "Choose an unused --path or the existing checkout of this repository")


def _updated_config(path: Path, name: str, checkout: str, mode: str) -> tuple[str, str]:
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    document = tomlkit.parse(original)
    repos = document.setdefault("repos", tomlkit.table())
    if name in repos and Path(str(repos[name])).expanduser().resolve() != Path(checkout):
        raise OnboardingError("config", f"{name} is already configured at another path", "Use its configured path or edit the existing repository entry")
    if name not in repos:
        repos[name] = checkout
        if mode == "disconnected":
            document.setdefault("repo_modes", tomlkit.table())[name] = mode
    parse_engine_config(document.unwrap())
    return original, tomlkit.dumps(document)


def persist_repository(path: Path, name: str, checkout: str, mode: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + ".lock"):
        original, updated = _updated_config(path, name, checkout, mode)
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
        await service.add_graph(project, source=starters.source(name, runner=runner), format="python")


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

    def _default_checkout(self, name: str) -> Path:
        directory = self.onboarding_directory if self.onboarding_directory is not None else Path.home() / "code"
        return (directory / name.split("/")[1]).expanduser().resolve()

    def start(self, reference: str, path: str | None, mode: str) -> dict:
        name = repository_name(reference)
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
            if previous["repository"] == name and previous["status"] == "complete" and previous["path"] == str(checkout) and checkout.is_dir():
                return {**previous, "message": "Already onboarded"}
        job = {"id": uuid4().hex, "repository": name, "path": str(checkout), "mode": mode,
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
                job.update(status="preflight", message="Checking git, GitHub SSH and agent sign-in")
                already_configured = name in self.registry.snapshot.repos
                problems, agent = await asyncio.to_thread(preflight)
                if problems:
                    job.update(status="failed", message="Preflight failed", problems=problems)
                    return
                await asyncio.to_thread(validate_checkout, name, path)
                _, configured = await asyncio.to_thread(_updated_config, self.config_path, name, str(path), mode)
                mode = tomlkit.parse(configured).get("repo_modes", {}).get(name, "connected")
                job["mode"] = mode
                job.update(status="cloning", message=f"Preparing checkout of {name}")
                await asyncio.to_thread(ensure_repository_checkouts, {name: str(path)})
                await asyncio.to_thread(validate_checkout, name, path)
                job.update(status="registering", message="Registering starter graphs")
                await self.register_starters(name, agent or "claude")
                job.update(status="configuring", message="Saving repository configuration")
                await asyncio.to_thread(persist_repository, self.config_path, name, str(path), mode)
                if name not in self.registry.snapshot.repos:
                    await asyncio.to_thread(self.registry.add, name, str(path), mode if mode == "disconnected" else None)
                job.update(status="complete", message="Already onboarded" if already_configured else "Repository onboarded")
            except OnboardingError as error:
                job.update(status="failed", message=str(error), problems=[error.problem])
            except Exception as error:  # noqa: BLE001 -- retain background failures for polling clients
                # Jobs must expose failures after the initiating HTTP request has ended.
                job.update(status="failed", message="Onboarding failed", problems=[{
                    "code": "onboarding", "message": str(error),
                    "fix": "Check the daemon's configuration, graph service, SSH access and destination permissions, then retry",
                }])

    def start_request(self, body: object) -> dict:
        if not isinstance(body, dict) or not isinstance(body.get("repository"), str):
            raise OnboardingError("repository", "Invalid repository", "Provide a GitHub URL or owner/repo")
        if body.get("path") is not None and (not isinstance(body["path"], str) or not body["path"].strip()):
            raise OnboardingError("path", "Invalid checkout path", "Provide a non-empty path on the daemon machine")
        mode = body.get("mode", "connected")
        if not isinstance(mode, str):
            raise OnboardingError("mode", "Invalid mode", "Choose connected or disconnected")
        return self.start(body["repository"], body.get("path"), mode)

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

        async def api_v1_start(request: Request) -> JSONResponse:
            if await visible_repositories(request) is not None:
                return JSONResponse({"error": "repository onboarding is for operators"}, status_code=403)
            try:
                body = await request.json()
                return JSONResponse(self.start_request(body), status_code=202)
            except OnboardingError as error:
                return JSONResponse({"status": "failed", "message": str(error), "problems": [error.problem]})
            except ValueError:
                return JSONResponse({"error": "invalid JSON request"}, status_code=400)

        async def api_v1_status(request: Request) -> JSONResponse:
            if await visible_repositories(request) is not None:
                return JSONResponse({"error": "repository onboarding is for operators"}, status_code=403)
            job = self.jobs.get(request.path_params["job_id"])
            if job is None:
                return JSONResponse({"error": "onboarding job not found; retry onboarding"}, status_code=404)
            return JSONResponse(dict(job))

        return [
            Route("/api/repositories/onboard", start, methods=["POST"]),
            Route("/api/repositories/onboard/{job_id}", status),
            Route("/api/v1/repositories/onboard", api_v1_start, methods=["POST"]),
            Route("/api/v1/repositories/onboard/{job_id}", api_v1_status),
        ]

    async def close(self) -> None:
        # Do not cancel a to_thread clone/config write while it is still running.
        if self.tasks:
            await asyncio.gather(*self.tasks)
