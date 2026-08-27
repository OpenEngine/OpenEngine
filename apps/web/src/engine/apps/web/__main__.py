"""Web control interface entrypoint.

The Python process serves both the chat API and the built assistant-ui client.
``--check`` retains the cheap composition smoke test used in CI.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from engine.apps.web.api import create_app
from engine.apps.web.composition import (
    Settings,
    build_capabilities,
    build_read_only_runners,
    build_runners,
    build_session,
    build_workflow_runners,
)
from engine.runtime import (
    EngineConfigError,
    LoadedEngineConfig,
    describe_loaded_config,
    load_engine_config,
    load_workflow_catalog,
    WorkflowLoadError,
)

#: Vite's production output, served by the same process as the API.
STATIC_DIRECTORY = Path(__file__).resolve().parents[4] / "dist"


def report_wiring(settings: Settings) -> None:
    """Print the composed capability graph, as the other two roots do."""
    capabilities = build_capabilities(settings)
    runners = build_runners(settings)
    read_only_runners = build_read_only_runners(settings)
    workflow_runners = build_workflow_runners(settings)
    session = build_session(capabilities, runners, read_only_runners=read_only_runners)
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
    # Named for what they are and what uses them: an operator reading this has
    # to be able to see that a planning chat runs on these too, not only a
    # workflow's review step.
    print(
        "read-only runners (workflow reviews, read-only agents): "
        + ", ".join(
            f"{name} ({type(runner).__name__})"
            for name, runner in read_only_runners.items()
        )
    )
    read_only_agents = sorted(
        agent_id for agent_id, profile in session.profiles.items() if profile.read_only
    )
    print(f"read-only agents: {', '.join(read_only_agents) or 'none'}")
    print(f"assistant-ui chat is live; conversations are stored in {settings.sqlite_path}.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the OpenEngine web interface.")
    parser.add_argument("--config", help="read Engine settings from this TOML file")
    parser.add_argument("--check", action="store_true", help="report wiring and exit")
    args = parser.parse_args(argv)
    try:
        loaded = load_engine_config(args.config)
        workflow_catalog = (
            load_workflow_catalog(loaded.workflows_directory)
            if loaded.workflows_directory is not None
            else None
        )
    except (EngineConfigError, WorkflowLoadError) as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    settings = Settings(engine_config=loaded.config, config_path=loaded.path)

    if args.check:
        report_wiring(settings)
        return 0

    capabilities = build_capabilities(settings)
    runners = build_runners(settings)
    read_only_runners = build_read_only_runners(settings)
    workflow_runners = build_workflow_runners(settings)
    session = build_session(capabilities, runners, read_only_runners=read_only_runners)
    app = create_app(
        session,
        runners,
        STATIC_DIRECTORY,
        workflow_runners=workflow_runners,
        review_runners=read_only_runners,
        workflow_catalog=workflow_catalog,
        approval_policy=loaded.config.approvals,
        default_branch=loaded.config.default_branch,
    )
    print(describe_loaded_config(loaded))
    uvicorn.run(app, host=settings.host, port=settings.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
