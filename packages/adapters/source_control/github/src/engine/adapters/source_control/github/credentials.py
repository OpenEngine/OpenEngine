"""Git credential protocol helper. Secrets travel only on its stdout pipe."""

import asyncio
import sys
from pathlib import Path

from .transports import GitHubAppTransport


def main() -> None:
    secret_file, operation = sys.argv[1:]
    if operation != "get":
        return
    fields = dict(line.rstrip("\n").split("=", 1) for line in sys.stdin if "=" in line)
    if fields.get("protocol") != "https" or fields.get("host") != "github.com":
        print("quit=true\n")
        return
    repository = fields.get("path", "").removesuffix(".git")
    if len(repository.split("/")) != 2 or any(part in {"", ".", ".."} for part in repository.split("/")):
        print("quit=true\n")
        return
    try:
        token = asyncio.run(GitHubAppTransport(Path(secret_file)).installation_token(repository))
    except Exception:
        # Never fall through to the host credential helper on app failure.
        print("quit=true\n")
        raise SystemExit(1)
    print(f"username=x-access-token\npassword={token}\n")


if __name__ == "__main__":
    main()
