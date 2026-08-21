"""A fake `gh` executable, and a record of everything it was asked to do.

`GitHubSourceControl` leaves review comments by shelling out to the GitHub CLI:
`gh pr comment` for a general one, and `gh pr view --json headRefOid` followed
by `gh api .../pulls/<n>/comments` for an inline one. Both of those are real
requests to somebody's repository, so a test that wants to assert on them needs
a `gh` that is not GitHub.

This is that: a shim installed next to the fake provider CLIs and named in
`Settings.github_binary`, which appends each invocation to a JSONL file the test
reads and answers with something the adapter can carry on from. `gh pr view
--json headRefOid --jq .headRefOid` in particular must print a commit SHA -- an
empty answer there is an error the adapter raises rather than a comment it
posts.

Named rather than shimmed onto `PATH` on purpose: a binary chosen through the
composition is visible in the composition report, and cannot be taken by
surprise the moment something else in the process wants the real `gh`.
"""

from __future__ import annotations

import json
import select
import shlex
import sys
from collections.abc import Sequence
from pathlib import Path

#: What `gh pr view --json headRefOid` answers with. A real SHA in shape, so a
#: caller that validates one is not the thing that fails.
HEAD_SHA = "b0dd4f0e3f16b98b4fcb52e39a1cd1ac2f5b1f2c"


def install(directory: Path, log: Path) -> str:
    """Write an executable `gh` into `directory`, recording into `log`."""

    path = directory / "gh"
    path.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} "
        f"{shlex.quote(str(Path(__file__).resolve()))} "
        f"{shlex.quote(str(log))} \"$@\"\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return str(path)


def calls(log: Path) -> list[dict[str, object]]:
    """Every invocation so far, in order, as `{argv, stdin}` records."""

    if not log.is_file():
        return []
    return [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _answer(arguments: Sequence[str]) -> str:
    """Plausible output for the calls the source-control adapter makes."""

    match tuple(arguments[:2]):
        case ("pr", "view"):
            return HEAD_SHA
        case ("pr", "comment"):
            url = arguments[2] if len(arguments) > 2 else "https://github.test/pull/1"
            return f"{url}#issuecomment-1"
        case ("api", _):
            return json.dumps({"id": 1})
        case _:
            return ""


def _stdin() -> str:
    """Whatever was piped in, and nothing when nobody is piping.

    The adapter passes everything on argv today, so this is empty in practice --
    but a `--body-file -` would put the comment here, and that is the half of an
    invocation argv would not have. Deliberately never blocks: a terminal is not
    input, and neither is a pipe nobody has written to.
    """

    if sys.stdin is None or sys.stdin.isatty():
        return ""
    if not select.select([sys.stdin], [], [], 0)[0]:
        return ""
    return sys.stdin.read()


def main(argv: Sequence[str]) -> int:
    if not argv:
        raise SystemExit(f"usage: {Path(__file__).name} <log> [gh arguments...]")
    log, arguments = Path(argv[0]), list(argv[1:])
    with log.open("a", encoding="utf-8") as recorded:
        recorded.write(json.dumps({"argv": arguments, "stdin": _stdin()}) + "\n")
    answer = _answer(arguments)
    if answer:
        print(answer)
    return 0


__all__ = ["HEAD_SHA", "calls", "install"]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
