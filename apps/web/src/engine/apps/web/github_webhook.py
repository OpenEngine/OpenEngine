"""Where a GitHub webhook delivery is checked against, and with what.

Split the way the login credentials are split: `engine.toml` names the
repository this deployment answers, and the shared secret that signs its
deliveries lives in a server-local `.env` beside that file. `engine.toml` is
committed, so a secret written there is published; the `.env` is gitignored and
is read directly, with dotenv interpolation disabled, rather than sourced into a
shell. An explicit process environment variable wins over the file, and the file
is reread per delivery so rotating the secret in GitHub and on disk takes effect
without a restart.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from engine.runtime import LoadedEngineConfig

SECRET_VARIABLE = "ENGINE_GITHUB_WEBHOOK_SECRET"


@dataclass(frozen=True)
class GitHubWebhookConfig:
    """The repository to accept deliveries from, and how to read their secret."""

    repository: str
    secret_file: Path | None = field(default=None, repr=False)

    def current_secret(self) -> str:
        """The secret as it is written right now, or empty when unconfigured.

        Empty rather than an error: a delivery that cannot be verified is
        refused by the handler, which is a state an operator can see and fix,
        while a startup failure would take the rest of the interface with it.
        """

        values = (
            {}
            if self.secret_file is None
            else dotenv_values(self.secret_file, interpolate=False)
        )
        secret = os.environ.get(SECRET_VARIABLE, values.get(SECRET_VARIABLE) or "")
        return secret.strip()


def github_webhook_config(loaded: LoadedEngineConfig) -> GitHubWebhookConfig | None:
    """Build the webhook configuration, or ``None`` when nobody asked for one.

    Half-configured still counts as configured: a repository without a secret,
    or a secret without a repository, is someone midway through the setup, and
    reporting that is more use than silently behaving as if neither was written.
    """

    secret_file = (loaded.path.parent if loaded.path else Path.cwd()) / ".env"
    config = GitHubWebhookConfig(loaded.config.github.repository, secret_file)
    if not config.repository and not config.current_secret():
        return None
    return config


__all__ = ["SECRET_VARIABLE", "GitHubWebhookConfig", "github_webhook_config"]
