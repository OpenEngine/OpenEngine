from __future__ import annotations

import argparse
import sys

from openengine.cli.commands import add_parsers


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oe", description="OpenEngine V2")
    add_parsers(parser.add_subparsers(required=True, metavar="command"))
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    return arguments.command(arguments)


if __name__ == "__main__":
    sys.exit(main())
