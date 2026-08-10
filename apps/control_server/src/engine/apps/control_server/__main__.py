"""Control server entrypoint: serves the planner UI."""

import sys

import uvicorn

from engine.apps.control_server.composition import Settings, build_agent_runner
from engine.runtime import available_agent_runners
from engine.web import PlannerService, create_app


def build_service(settings: Settings) -> PlannerService:
    runner, name = build_agent_runner(settings)
    return PlannerService(
        runner,
        workspace_root=settings.workspace_root,
        backend=name,
        model=settings.model,
    )


def main(argv: list[str] | None = None) -> None:
    """Serve the planner. `--check` wires everything up and exits instead.

    The check mode exists for CI: starting the real server would block forever,
    but "does the composition graph still build" is exactly the failure a smoke
    test should catch -- a bad constructor signature breaks nothing else.
    """
    args = sys.argv[1:] if argv is None else argv
    check_only = "--check" in args

    settings = Settings.from_env()
    service = build_service(settings)
    app = create_app(service)

    installed = sorted(available_agent_runners())
    print("engine planner")
    print(f"  agent backend : {service.backend}" + (f" ({service.model})" if service.model else ""))
    print(f"  installed     : {', '.join(installed) or 'none'}")
    print(f"  workspace     : {settings.workspace_root}")
    if service.backend == "scripted":
        print("  note          : running the offline demo script.")
        print("                  `ant auth login` or ANTHROPIC_API_KEY selects anthropic.")

    if check_only:
        print(f"  routes        : {len(app.routes)} wired")
        print("  --check       : composition graph builds; not serving.")
        return

    print(f"  listening on  : http://{settings.host}:{settings.port}")
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")


if __name__ == "__main__":
    main()
