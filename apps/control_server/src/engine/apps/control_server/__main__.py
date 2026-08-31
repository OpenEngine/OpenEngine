"""Control server entrypoint.

Ticket 1 stops at composition: it builds the capability graph and reports it.
The HTTP surface that accepts run requests lands with the control-server ticket.
"""

import argparse
import os
import sys
from collections.abc import Sequence

from engine.apps.control_server.composition import Settings, build_capabilities
from engine.runtime import (
    EngineConfigError,
    WorkflowLoadError,
    describe_loaded_config,
    load_engine_config,
    load_workflow_catalog,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the OpenEngine control server.")
    parser.add_argument("--config", help="read Engine settings from this TOML file")
    args = parser.parse_args(argv)
    try:
        loaded = load_engine_config(args.config)
        if loaded.workflows_directory is not None:
            load_workflow_catalog(loaded.workflows_directory)
    except (EngineConfigError, WorkflowLoadError) as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    capabilities = build_capabilities(
        Settings(
            engine_config=loaded.config,
            config_path=loaded.path,
            github_token=os.environ.get("GITHUB_TOKEN", loaded.config.github_token),
        )
    )
    print(describe_loaded_config(loaded))
    print("engine control server -- capabilities wired:")
    for field in type(capabilities).__dataclass_fields__:
        print(f"  {field}: {type(getattr(capabilities, field)).__name__}")
    print("no ingress yet; see Ticket 1 acceptance criteria.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
