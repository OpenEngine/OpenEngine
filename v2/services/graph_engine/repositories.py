"""Read-only local discovery. Never read imported configuration or acquire a branch."""
from __future__ import annotations

import asyncio
from pathlib import Path

from openengine.messaging.errors import invalid_argument


async def discover(checkout: str, base_ref: str) -> str:
    path = Path(checkout)
    if not path.is_absolute() or not path.is_dir():
        raise invalid_argument([("payload.checkout", "must be an existing absolute local directory")])

    async def read(*arguments: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "git", "--no-optional-locks", "-C", str(path.resolve()), *arguments,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), 5)
        except TimeoutError:
            process.kill()
            await process.communicate()
            raise invalid_argument([("payload.checkout", "local discovery timed out")]) from None
        if process.returncode:
            raise invalid_argument([("payload.checkout", "must be a working checkout with a valid base ref")])
        return stdout.decode().rstrip("\n")

    try:
        if await read("rev-parse", "--is-inside-work-tree") != "true":
            raise invalid_argument([("payload.checkout", "bare repositories are not supported")])
        root = await read("rev-parse", "--show-toplevel")
        await read("rev-parse", "--verify", "--end-of-options", base_ref + "^{commit}")
    except (OSError, UnicodeError):
        raise invalid_argument([("payload.checkout", "cannot discover local checkout")]) from None
    return str(Path(root).resolve())
