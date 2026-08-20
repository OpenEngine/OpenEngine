"""``openengine``: the command an installed OpenEngine answers to.

The installed product is one command with subcommands rather than a family of
``engine-*`` scripts, because the things still to land here -- ``doctor``, and
eventually the worker -- are verbs, and a verb is a subcommand rather than a
separate program on someone's ``PATH``. Bare ``openengine`` is ``openengine
web`` for the same reason: the interface is what the product is.

``engine-web`` and its siblings stay exactly as they were. Operators who
already run them have a name that works, and this module holds the serving code
both of them reach.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from engine.apps.web.api import create_app
from engine.apps.web.build import build_info
from engine.apps.web.composition import (
    Settings,
    build_capabilities,
    build_review_runners,
    build_runners,
    build_session,
    build_workflow_runners,
)
from engine.runtime import (
    EngineConfigError,
    LoadedEngineConfig,
    describe_loaded_config,
    load_engine_config,
)

#: Vite's production output, shipped as package data beside this module. A
#: checkout builds into the same place an archive unpacks it to, so the server
#: finds its client the same way in both -- relative to itself, never relative
#: to a repository that may not be there.
STATIC_DIRECTORY = Path(__file__).resolve().parent / "client"

#: Subcommands, and the flags that belong to `openengine` rather than to one of
#: them. Anything else is arguments for the default subcommand.
COMMANDS = ("web",)
TOP_LEVEL_FLAGS = ("-h", "--help", "--version")


def report_wiring(settings: Settings) -> None:
    """Print the composed capability graph, as the other two roots do."""
    capabilities = build_capabilities(settings)
    runners = build_runners(settings)
    review_runners = build_review_runners(settings)
    workflow_runners = build_workflow_runners(settings)
    session = build_session(capabilities, runners)
    print(
        describe_loaded_config(
            LoadedEngineConfig(config=settings.engine_config, path=settings.config_path)
        )
    )
    print(f"openengine web -- http://{settings.host}:{settings.port}, capabilities wired:")
    for field in type(capabilities).__dataclass_fields__:
        print(f"  {field}: {type(getattr(capabilities, field)).__name__}")
    print(f"agents: {', '.join(sorted(session.profiles))}")
    print(f"runners: {', '.join(f'{n} ({type(r).__name__})' for n, r in runners.items())}")
    print(
        "workflow runners: "
        + ", ".join(
            f"{name} ({type(runner).__name__})"
            for name, runner in workflow_runners.items()
        )
    )
    print(
        "review runners: "
        + ", ".join(
            f"{name} ({type(runner).__name__})"
            for name, runner in review_runners.items()
        )
    )
    print(f"assistant-ui chat is live; conversations are stored in {settings.sqlite_path}.")


def serve(
    config: str | None = None,
    check: bool = False,
    host: str | None = None,
    port: int | None = None,
) -> int:
    """Start the web interface, or report its wiring and stop.

    ``check`` retains the cheap composition smoke test used in CI.

    ``host`` and ``port`` default to loopback on 8000. They are options rather
    than settings because the thing that needs to move them is a caller, not a
    deployment: a smoke test on a machine that already has something on 8000,
    an installer checking that the command it just installed answers.
    """
    try:
        loaded = load_engine_config(config)
    except EngineConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    binding: dict[str, object] = {}
    if host is not None:
        binding["host"] = host
    if port is not None:
        binding["port"] = port
    settings = Settings(
        engine_config=loaded.config, config_path=loaded.path, **binding
    )

    if check:
        report_wiring(settings)
        return 0

    capabilities = build_capabilities(settings)
    runners = build_runners(settings)
    review_runners = build_review_runners(settings)
    workflow_runners = build_workflow_runners(settings)
    session = build_session(capabilities, runners)
    app = create_app(
        session,
        runners,
        STATIC_DIRECTORY,
        workflow_runners=workflow_runners,
        review_runners=review_runners,
        approval_policy=loaded.config.approvals,
    )
    print(describe_loaded_config(loaded))
    print(f"conversations are stored in {settings.sqlite_path}")
    uvicorn.run(app, host=settings.host, port=settings.port)
    return 0


def parser() -> argparse.ArgumentParser:
    """The `openengine` command line."""
    command = argparse.ArgumentParser(prog="openengine", description="Run OpenEngine.")
    command.add_argument(
        "--version",
        action="store_true",
        help="print the version and the commit it was built from",
    )
    subcommands = command.add_subparsers(dest="command")
    web = subcommands.add_parser("web", help="start the web interface (the default)")
    add_serving_arguments(web)
    return command


def add_serving_arguments(parser: argparse.ArgumentParser) -> None:
    """The options `openengine web` and `engine-web` both take."""
    parser.add_argument("--config", help="read Engine settings from this TOML file")
    parser.add_argument("--check", action="store_true", help="report wiring and exit")
    parser.add_argument("--host", help="interface to bind (default: localhost)")
    parser.add_argument("--port", type=int, help="port to bind (default: 8000)")


def with_default_command(arguments: Sequence[str]) -> list[str]:
    """`openengine` and `openengine --config x` mean `openengine web ...`."""
    head = arguments[0] if arguments else None
    if head in COMMANDS or head in TOP_LEVEL_FLAGS:
        return list(arguments)
    return ["web", *arguments]


def main(argv: Sequence[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    args = parser().parse_args(with_default_command(arguments))
    if args.version:
        print(build_info())
        return 0
    return serve(
        config=args.config, check=args.check, host=args.host, port=args.port
    )


if __name__ == "__main__":
    raise SystemExit(main())
