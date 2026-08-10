"""Web control interface entrypoint.

Streamlit owns its own server and wants to be handed a script path, so this is a
thin front: build settings, hand `app.py` over, and pass any extra flags
straight through (`engine-web --server.headless true`).

    engine-web            # serve the interface
    engine-web --check    # compose, report, exit

`--check` exists so this composition root gets the same cheap smoke test in CI
that the other two get from simply running: a bad adapter constructor signature
is not caught by any test, because `composition.py` is the one place adapters are
named. Starting a server would block, so the check stops just before that.
"""

import subprocess
import sys
from pathlib import Path

from engine.apps.web.composition import Settings, build_capabilities, build_session

#: The script Streamlit runs. A sibling file, not a package entry point --
#: Streamlit reruns it top to bottom on every interaction.
APP_SCRIPT = Path(__file__).with_name("app.py")


def report_wiring(settings: Settings) -> None:
    """Print the composed capability graph, as the other two roots do."""
    capabilities = build_capabilities(settings)
    session = build_session(capabilities)
    print(f"engine web -- http://{settings.host}:{settings.port}, capabilities wired:")
    for field in type(capabilities).__dataclass_fields__:
        print(f"  {field}: {type(getattr(capabilities, field)).__name__}")
    print(f"agents: {', '.join(sorted(session.profiles))}")
    print("chat is live; the run pages read nothing yet.")


def main() -> int:
    settings = Settings()
    args = sys.argv[1:]

    if "--check" in args:
        report_wiring(settings)
        return 0

    return subprocess.call(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(APP_SCRIPT),
            "--server.address",
            settings.host,
            "--server.port",
            str(settings.port),
            *args,
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
