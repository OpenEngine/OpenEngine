"""Apply GitHub App identity and authentication only to an Engine worktree."""

import shlex
import sys
from collections.abc import Awaitable, Callable

from .transports import GitHubAppTransport


async def configure_worktree(
    app: GitHubAppTransport, path: str, git: Callable[..., Awaitable[str]],
) -> None:
    remote = await git(path, "config", "--get", "remote.origin.url")
    if not remote.startswith(("git@github.com:", "https://github.com/", "ssh://git@github.com/")):
        return
    login, email = await app.bot_identity()
    helper = "!" + shlex.join([
        sys.executable, "-m", "engine.adapters.source_control.github.credentials",
        str(app.secret_file),
    ])
    # The provider enables extensions.worktreeConfig alongside its existing
    # co-author hook. All identity and credential settings stay worktree-local.
    settings = (
        ("user.name", login), ("user.email", email),
        ("credential.helper", ""),
        ("credential.helper", helper),
        ("credential.useHttpPath", "true"),
        ("url.https://github.com/.insteadOf", "git@github.com:"),
        ("url.https://github.com/.insteadOf", "ssh://git@github.com/"),
    )
    for key in {key for key, _ in settings}:
        # --replace-all also makes reattaching an existing worktree idempotent.
        values = [value for name, value in settings if name == key]
        await git(path, "config", "--worktree", "--replace-all", key, values[0])
        for value in values[1:]:
            await git(path, "config", "--worktree", "--add", key, value)
