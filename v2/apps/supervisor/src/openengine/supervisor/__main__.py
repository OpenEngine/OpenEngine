from __future__ import annotations

import argparse
import sys
from pathlib import Path

from openengine.supervisor.supervisor import Settings, run
from openengine.telemetry import configure


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oe-supervisor", description="run OpenEngine V2's local processes")
    parser.add_argument("--state-dir", type=Path, required=True, help="where state, the state file and NATS data live")
    parser.add_argument("--cache-dir", type=Path, required=True, help="where downloaded binaries are cached")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--nats-port", type=int, required=True)
    parser.add_argument("--nats-monitor-port", type=int, required=True)
    parser.add_argument("--nats-server", type=Path, help="use this nats-server binary instead of the pinned download")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    telemetry = configure("supervisor")
    try:
        return run(
            Settings(
                state_directory=arguments.state_dir,
                cache_directory=arguments.cache_dir,
                host=arguments.host,
                nats_port=arguments.nats_port,
                nats_monitor_port=arguments.nats_monitor_port,
                nats_server=arguments.nats_server,
            )
        )
    finally:
        telemetry.shutdown()


if __name__ == "__main__":
    sys.exit(main())
