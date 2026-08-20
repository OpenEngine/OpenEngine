"""``engine-web``: the operator-facing name for ``openengine web``.

Kept as its own console script, and deliberately thin. The program lives in
``cli``, which is where ``openengine`` reaches it too, so the two names cannot
drift into two behaviours.
"""

import argparse
from collections.abc import Sequence

from engine.apps.web.cli import add_serving_arguments, serve


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the OpenEngine web interface.")
    add_serving_arguments(parser)
    args = parser.parse_args(argv)
    return serve(
        config=args.config, check=args.check, host=args.host, port=args.port
    )


if __name__ == "__main__":
    raise SystemExit(main())
