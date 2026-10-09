"""Which engine daemon a command talks to.

A backend is a name for a daemon's URL -- this machine's, or the Mac mini's --
plus what a command should assume there: the project names resolve in, the
repository a run checks out when none is given, and which environment variable
holds the bearer token. The token itself is never written to the file.

    engine backend add mini http://mac-mini.local:4364 --token-env MINI_ENGINE_TOKEN --use
    engine backends list
    engine graph run triage "..." --backend local

With nothing configured there is one backend, `local`, at the daemon's default
address, so a fresh install works without any of this.
"""

from __future__ import annotations

import ipaddress
import json
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from platformdirs import user_config_path

LOCAL = "local"
DEFAULT_URL = "http://127.0.0.1:4364"
DEFAULT_PROJECT = "default"
FILE_ENVIRONMENT_VARIABLE = "ENGINE_BACKENDS_FILE"
SELECTED_ENVIRONMENT_VARIABLE = "ENGINE_BACKEND"
TOKEN_ENVIRONMENT_VARIABLE = "ENGINE_SERVICE_TOKEN"


class BackendError(ValueError):
    pass


@dataclass(frozen=True)
class Backend:
    name: str
    url: str
    project: str = DEFAULT_PROJECT
    token_env: str = ""
    """The environment variable holding this backend's bearer token, if any."""
    repository: str = ""
    """What a run checks out when neither the command nor the graph says."""

    def token(self) -> str:
        if self.token_env:
            return os.environ.get(self.token_env, "")
        return os.environ.get(TOKEN_ENVIRONMENT_VARIABLE, "")

    @property
    def is_local(self) -> bool:
        host = urlsplit(self.url).hostname or ""
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def json(self, *, current: bool = False) -> dict[str, object]:
        return {**asdict(self), "current": current}


@dataclass(frozen=True)
class Backends:
    current: str = LOCAL
    backends: dict[str, Backend] = field(default_factory=dict)

    def all(self) -> dict[str, Backend]:
        """Every backend, `local` included even when it was never added."""
        return {LOCAL: Backend(LOCAL, DEFAULT_URL), **self.backends}

    def get(self, name: str) -> Backend:
        found = self.all().get(name)
        if found is None:
            raise BackendError(f"no backend named {name!r}; see `engine backends list`")
        return found

    def selected(self, override: str | None = None) -> Backend:
        """`--backend`, else `ENGINE_BACKEND`, else the one `backend use` chose."""
        return self.get(override or os.environ.get(SELECTED_ENVIRONMENT_VARIABLE) or self.current)

    @property
    def configured(self) -> bool:
        """Whether a backend other than `local` was chosen, so it outranks older settings."""
        return self.current != LOCAL


def path() -> Path:
    override = os.environ.get(FILE_ENVIRONMENT_VARIABLE)
    return Path(override) if override else user_config_path("openengine") / "backends.json"


def load() -> Backends:
    try:
        raw = json.loads(path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Backends()
    except (OSError, ValueError) as error:
        raise BackendError(f"cannot read {path()}: {error}") from None
    if not isinstance(raw, dict):
        raise BackendError(f"{path()} must hold a JSON object")
    backends: dict[str, Backend] = {}
    for name, value in (raw.get("backends") or {}).items():
        if isinstance(value, dict):
            known = {key: value[key] for key in ("url", "project", "token_env", "repository") if key in value}
            backends[name] = Backend(name=name, **known)
    current = raw.get("current") if isinstance(raw.get("current"), str) else LOCAL
    return Backends(current=current, backends=backends)


def save(config: Backends) -> None:
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "current": config.current,
        "backends": {
            name: {key: value for key, value in asdict(backend).items() if key != "name"}
            for name, backend in config.backends.items()
        },
    }
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)


def normalize_url(value: str) -> str:
    value = value.strip()
    if "://" not in value:
        value = f"http://{value}"
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BackendError(f"{value!r} is not an http(s) URL")
    if parts.query or parts.fragment:
        raise BackendError("a backend URL cannot have a query or fragment")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def add(
    name: str,
    url: str,
    *,
    project: str = DEFAULT_PROJECT,
    token_env: str = "",
    repository: str = "",
    use: bool = False,
    replace_existing: bool = False,
) -> Backends:
    name = name.strip()
    if not name or not name.replace("-", "").replace("_", "").isalnum():
        raise BackendError("a backend name is letters, digits, '-' and '_'")
    config = load()
    if name in config.backends and not replace_existing:
        raise BackendError(f"backend {name!r} already exists; pass --replace to change it")
    if token_env and not token_env.replace("_", "").isalnum():
        raise BackendError("--token-env names an environment variable, not a token")
    backend = Backend(
        name, normalize_url(url), project.strip() or DEFAULT_PROJECT, token_env.strip(), repository.strip()
    )
    updated = replace(
        config,
        backends={**config.backends, name: backend},
        current=name if use else config.current,
    )
    save(updated)
    return updated


def remove(name: str) -> Backends:
    config = load()
    if name not in config.backends:
        raise BackendError(f"no added backend named {name!r}")
    backends = {key: value for key, value in config.backends.items() if key != name}
    updated = Backends(current=LOCAL if config.current == name else config.current, backends=backends)
    save(updated)
    return updated


def use(name: str) -> Backends:
    config = load()
    config.get(name)
    updated = replace(config, current=name)
    save(updated)
    return updated


__all__ = [
    "Backend",
    "BackendError",
    "Backends",
    "DEFAULT_URL",
    "LOCAL",
    "add",
    "load",
    "normalize_url",
    "path",
    "remove",
    "save",
    "use",
]
