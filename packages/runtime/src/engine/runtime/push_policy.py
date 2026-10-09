"""Closed parsing of push options before granting permission to rewrite refs."""
from collections.abc import Sequence


def push_spec(arguments: Sequence[str]) -> tuple[str, tuple[str, ...], bool]:
    positional = []
    force = False
    options = True
    for argument in arguments[1:]:
        if options and argument == "--":
            options = False
        elif options and argument.startswith("-"):
            if argument in {"-f", "--force", "--force-with-lease", "--force-if-includes"} or argument.startswith("--force-with-lease="):
                force = True
            elif argument not in {"-u", "--set-upstream", "--porcelain", "--atomic", "-v", "--verbose", "-q", "--quiet", "--dry-run", "-n", "--no-verify"}:
                raise ValueError("unsupported push option; use explicit options and branch refspecs")
        else:
            positional.append(argument)
    if len(positional) < 2:
        raise ValueError("push requires an explicit remote and branch refspec")
    branches = []
    for refspec in positional[1:]:
        force |= refspec.startswith("+")
        source, sep, destination = refspec.lstrip("+").partition(":")
        destination = destination if sep else source
        if not source or not destination or "*" in refspec or destination in {"HEAD", "@"}:
            raise ValueError("push requires explicit source and destination branches")
        if destination.startswith("refs/") and not destination.startswith("refs/heads/"):
            raise ValueError("push destination must be a branch")
        branches.append(destination.removeprefix("refs/heads/"))
    return positional[0], tuple(branches), force
