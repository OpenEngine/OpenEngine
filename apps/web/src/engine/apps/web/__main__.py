"""Web control interface entrypoint.

The Python process serves both the chat API and the built assistant-ui client.
``--check`` retains the cheap composition smoke test used in CI.
"""

import sys
from pathlib import Path

import uvicorn

from engine.apps.web.api import create_app
from engine.apps.web.composition import (
    Settings,
    build_capabilities,
    build_runners,
    build_session,
)

#: Vite's production output, served by the same process as the API.
STATIC_DIRECTORY = Path(__file__).resolve().parents[4] / "dist"


def report_wiring(settings: Settings) -> None:
    """Print the composed capability graph, as the other two roots do."""
    capabilities = build_capabilities(settings)
    runners = build_runners(settings)
    session = build_session(capabilities, runners)
    print(f"engine web -- http://{settings.host}:{settings.port}, capabilities wired:")
    for field in type(capabilities).__dataclass_fields__:
        print(f"  {field}: {type(getattr(capabilities, field)).__name__}")
    print(f"agents: {', '.join(sorted(session.profiles))}")
    print(f"runners: {', '.join(f'{n} ({type(r).__name__})' for n, r in runners.items())}")
    print("assistant-ui chat is live; conversations are kept in memory.")


def main() -> int:
    settings = Settings()
    args = sys.argv[1:]

    if args == ["--check"]:
        report_wiring(settings)
        return 0
    if args:
        print(f"unknown arguments: {' '.join(args)}", file=sys.stderr)
        return 2

    capabilities = build_capabilities(settings)
    runners = build_runners(settings)
    session = build_session(capabilities, runners)
    app = create_app(session, runners, STATIC_DIRECTORY)
    uvicorn.run(app, host=settings.host, port=settings.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
