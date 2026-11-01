"""List every way each service's schema falls short of being tenant-scoped; exits 1 if any does.

    python -m openengine.persistence.tenancy --services services
"""

from __future__ import annotations

import argparse
import importlib
from collections.abc import Sequence
from pathlib import Path

from openengine.persistence.tenancy import REGISTRY, assert_tenant_scoped

def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--services", type=Path, default=Path("services"))
    parser.add_argument("--registry", type=Path, default=REGISTRY)
    arguments = parser.parse_args(argv)
    failed = False
    for package in sorted(arguments.services.glob("*/persistence/schema.py")):
        directory = package.parent.parent.name
        persistence = importlib.import_module(f"{directory}.persistence").PERSISTENCE
        schema = importlib.import_module(f"{directory}.persistence.schema")
        for violation in assert_tenant_scoped(schema.metadata, persistence.name, arguments.registry):
            print(f"{directory}: {violation}")
            failed = True
    if not failed:
        print("every table is tenant-scoped")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
