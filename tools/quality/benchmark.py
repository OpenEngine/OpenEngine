"""Reproduce prototype cost: --root REPO, or --large for three ~1 MB files."""

import argparse
import json
from pathlib import Path
import resource
import sys
import tempfile
import time
import tomllib

from duplicates import scan, fuzzy_groups, prune_nested, scope


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--root", type=Path)
    mode.add_argument("--large", action="store_true")
    parser.add_argument("--threshold", type=float, default=.8)
    args = parser.parse_args()
    config = tomllib.loads(Path(__file__).with_name("duplicates.toml").read_text())
    with tempfile.TemporaryDirectory() as directory:
        root = args.root.resolve() if args.root else Path(directory)
        if args.large:
            source = "def work(value):\n" + "".join(
                f"    value = transform(value, {i})\n" for i in range(30000))
            for name in ("one", "two", "three"):
                (root / f"{name}.py").write_text(source)
        start = time.monotonic()
        result = scan(root, config)
        fuzzy = prune_nested(fuzzy_groups(result["units"], args.threshold))
        direct = prune_nested({k: v for k, v in result["groups"].items() if len(scope(v)) >= 3})
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        print(json.dumps({"seconds": time.monotonic() - start,
                          "peak_mib": peak / (1024 ** 2 if sys.platform == "darwin" else 1024),
                          "files": result["files"], "bytes": result["bytes"],
                          "groups": len(result["groups"]), "threshold": args.threshold,
                          "exact_groups": sum(g["kind"] == "exact" for g in direct.values()),
                          "type2_groups": sum(g["kind"] == "type2" for g in direct.values()),
                          "type3_groups": len(fuzzy), "errors": result["errors"],
                          "parse_exceptions": result["parse_exceptions"]}))


if __name__ == "__main__":
    main()
