"""Read one selected model login; never load a host configuration directory.

Selected values enter only the guest's tmpfs home. The credential lease may
retain validated Codex rotation in the selected host login. Discovery is a
composition-root capability, not an implicit action of a library provider.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Literal

from engine.ports.sandbox_home import SandboxHome

ModelProvider = Literal["codex", "claude"]
MAX_CREDENTIAL_BYTES = 64 * 1024
_CODEX_TOKENS = ("access_token", "id_token", "refresh_token", "account_id")


class ModelCredentialError(ValueError):
    """A selected provider has no usable, safely transferable login."""


@dataclass(frozen=True, slots=True)
class ModelCredential:
    provider: ModelProvider
    value: str = field(repr=False)
    kind: Literal["login", "api-key"] = "login"
    source_path: Path | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.provider not in {"codex", "claude"} or self.kind not in {"login", "api-key"}:
            message = "unsupported model credential type"
            raise ModelCredentialError(message)
        if not isinstance(self.value, str) or not self.value.strip() or len(self.value.encode()) > MAX_CREDENTIAL_BYTES or "\0" in self.value:
            message = "invalid selected model credential"
            raise ModelCredentialError(message)
        if self.provider == "claude" or self.kind == "api-key":
            object.__setattr__(self, "value", self.value.strip())
        if self.provider == "codex" and self.kind == "login":
            try:
                original = json.loads(self.value)
            except ValueError:
                original = None
            if not isinstance(original, dict):
                message = "invalid Codex auth.json"
                raise ModelCredentialError(message)
            tokens = original.get("tokens")
            clean: dict[str, object] = {}
            if isinstance(tokens, dict) and isinstance(tokens.get("access_token"), str) and tokens["access_token"]:
                clean["tokens"] = {key: tokens[key] for key in _CODEX_TOKENS if isinstance(tokens.get(key), str)}
                if isinstance(original.get("auth_mode"), str) and original["auth_mode"] in {"chatgpt", "apikey", "chatgptAuthTokens"}:
                    clean["auth_mode"] = original["auth_mode"]
                refreshed = original.get("last_refresh")
                if isinstance(refreshed, str):
                    try:
                        datetime.fromisoformat(refreshed)
                    except ValueError:
                        pass
                    else:
                        clean["last_refresh"] = refreshed
            elif isinstance(original.get("OPENAI_API_KEY"), str) and original["OPENAI_API_KEY"]:
                clean["OPENAI_API_KEY"] = original["OPENAI_API_KEY"]
            else:
                message = "Codex auth.json contains no supported model login"
                raise ModelCredentialError(message)
            object.__setattr__(self, "value", json.dumps(clean))

    def home(self) -> SandboxHome:
        if self.provider == "codex":
            value = json.dumps({"OPENAI_API_KEY": self.value}) if self.kind == "api-key" else self.value
            auth = json.loads(value)
            tokens = auth.get("tokens", {})
            secret_tokens = [value for key, value in tokens.items() if key != "account_id"]
            redactions = tuple(str(token) for token in (*secret_tokens, auth.get("OPENAI_API_KEY")) if token)
            return SandboxHome(
                files={".codex/auth.json": value, ".codex/config.toml": 'cli_auth_credentials_store = "file"\n'},
                paths={"CODEX_HOME": ".codex"}, redactions=redactions,
            )
        variable = "ANTHROPIC_API_KEY" if self.kind == "api-key" else "CLAUDE_CODE_OAUTH_TOKEN"
        return SandboxHome(
            files={".claude/model-token": self.value}, paths={"CLAUDE_CONFIG_DIR": ".claude"},
            env_files={variable: ".claude/model-token"}, redactions=(self.value,),
        )


ModelCredentialSource = Callable[[ModelProvider, Mapping[str, str]], ModelCredential]


def explicit_model_credential(provider: ModelProvider, values: Mapping[str, str]) -> ModelCredential | None:
    """Explicit per-node keys need no access to the host's login stores."""
    if provider == "codex" and (key := values.get("OPENAI_API_KEY")):
        return ModelCredential(provider, key, "api-key")
    if provider == "claude":
        if token := values.get("CLAUDE_CODE_OAUTH_TOKEN"):
            return ModelCredential(provider, token)
        if key := values.get("ANTHROPIC_API_KEY"):
            return ModelCredential(provider, key, "api-key")
    return None


def _host_home() -> Path:
    return Path.home()


def _read(path: Path) -> str | None:
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_CREDENTIAL_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError:
        message = "selected model credential file is unreadable"
        raise ModelCredentialError(message) from None
    if len(data) > MAX_CREDENTIAL_BYTES:
        message = "selected model credential exceeds byte budget"
        raise ModelCredentialError(message)
    try:
        return data.decode("utf-8")
    except UnicodeError:
        message = "selected model credential is not UTF-8"
        raise ModelCredentialError(message) from None


def _claude_token(value: str | None) -> tuple[str, float] | None:
    try:
        data = json.loads(value or "")
    except ValueError:
        return None
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict) or not isinstance(oauth.get("accessToken"), str) or not oauth["accessToken"]:
        return None
    expires = oauth.get("expiresAt", 0)
    expiry = float(expires) / 1000 if isinstance(expires, (int, float)) else 0
    if expiry and expiry <= time.time():
        return None
    return oauth["accessToken"], expiry


def _claude_keychain() -> str | None:
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode or len(result.stdout) > MAX_CREDENTIAL_BYTES:
        return None
    return result.stdout.decode(errors="replace")


def host_model_credential(provider: ModelProvider, overrides: Mapping[str, str]) -> ModelCredential:
    """Resolve only the selected provider, respecting explicit node/account choices."""
    def selected(key: str) -> str:
        return overrides.get(key, os.environ.get(key, ""))
    if explicit := explicit_model_credential(provider, overrides):
        return explicit
    if provider == "codex":
        root = Path(selected("CODEX_HOME") or _host_home() / ".codex").expanduser()
        if value := _read(root / "auth.json"):
            return ModelCredential(provider, value, source_path=(root / "auth.json").resolve())
        if key := selected("OPENAI_API_KEY"):
            return ModelCredential(provider, key, "api-key")
        message = "Codex file login is unavailable; sign in using file credential storage or supply a selected API key"
        raise ModelCredentialError(message)
    if provider != "claude":
        message = "unsupported model credential provider"
        raise ModelCredentialError(message)
    if token := selected("CLAUDE_CODE_OAUTH_TOKEN"):
        return ModelCredential(provider, token)
    if key := selected("ANTHROPIC_API_KEY"):
        return ModelCredential(provider, key, "api-key")
    directory = Path(selected("CLAUDE_CONFIG_DIR") or _host_home() / ".claude").expanduser()
    candidates = [_claude_token(_read(directory / ".credentials.json"))]
    # A custom account directory must never fall back to a different account.
    if directory.resolve() == (_host_home() / ".claude").resolve():
        candidates.append(_claude_token(_claude_keychain()))
    available = sorted((item for item in candidates if item is not None), key=lambda item: item[1], reverse=True)
    if available:
        return ModelCredential(provider, available[0][0])
    message = "Claude login is unavailable or expired; sign in to the selected account or supply CLAUDE_CODE_OAUTH_TOKEN"
    raise ModelCredentialError(message)
