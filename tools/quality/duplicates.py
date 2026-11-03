"""Exact syntax clone detector v1. JSON on stdout; policy diagnostics on stderr.

No Git subprocesses: callers supply a separate checkout of the intended base.
"""

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import tomllib

from tree_sitter_language_pack import get_parser


LANGUAGES = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx",
    ".sh": "bash", ".rs": "rust", ".go": "go",
}
# Literal subtrees are atomic: whitespace and comment-like text inside them matters.
LITERALS = {"string", "string_literal", "template_string", "raw_string_literal"}
PARSE_ERROR = "Tree-sitter parse error (file was not scanned)"


@dataclass(frozen=True)
class Location:
    path: str
    start: int
    end: int
    start_byte: int
    end_byte: int


def fingerprints(data, language, minimum):
    """Merkle hashes of exact subtrees; iterative, linear in syntax tree size."""
    tree = get_parser(language).parse(data)
    if tree.root_node.has_error:
        raise ValueError(PARSE_ERROR)
    stack = [(tree.root_node, False)]
    values = {}
    while stack:
        node, visited = stack.pop()
        if "comment" in node.type:
            values[node.id] = (b"", 0)
            continue
        atomic = not node.children or node.type in LITERALS
        if not visited and not atomic:
            stack.append((node, True))
            stack.extend((child, False) for child in reversed(node.children))
            continue
        digest = hashlib.sha256()
        digest.update(node.type.encode() + b"\0")
        count = 1
        if atomic:
            digest.update(data[node.start_byte:node.end_byte])
        else:
            count = 0
            for child in node.children:
                child_hash, child_count = values.pop(child.id)
                digest.update(child_hash)
                count += child_count
        value = digest.digest()
        values[node.id] = (value, count)
        if count >= minimum:
            yield value.hex(), count, node


def scan(root, config):
    groups = defaultdict(list)
    sizes = {}
    errors, unsupported, parse_exceptions = [], [], []
    files = total_bytes = 0
    excluded_dirs = set(config["exclude_dirs"])
    excluded_paths = config["exclude_paths"]
    for source in config["roots"]:
        for directory, dirs, names in os.walk(
            root / source, followlinks=False,
            onerror=lambda error: errors.append({"path": str(error.filename), "error": str(error)}),
        ):
            dirs[:] = sorted(d for d in dirs if d not in excluded_dirs
                             and not (Path(directory) / d).is_symlink())
            for name in sorted(names):
                path = Path(directory) / name
                relative = path.relative_to(root).as_posix()
                if path.is_symlink() or any(
                    relative == p or relative.startswith(p + "/") for p in excluded_paths
                ):
                    continue
                language = LANGUAGES.get(path.suffix)
                if language is None:
                    unsupported.append(relative)
                    continue
                try:
                    data = path.read_bytes()
                    files += 1
                    total_bytes += len(data)
                    for key, size, node in fingerprints(data, language, config["min_tokens"]):
                        # Never group across grammars.
                        key = language + ":" + key
                        sizes[key] = size
                        groups[key].append(Location(relative, node.start_point.row + 1,
                                                    node.end_point.row + 1,
                                                    node.start_byte, node.end_byte))
                except (OSError, ValueError) as error:
                    waiver = config.get("parse_exceptions", {}).get(relative, {})
                    if (isinstance(error, ValueError) and waiver.get("rationale", "").strip()
                            and waiver.get("sha256") == hashlib.sha256(data).hexdigest()):
                        parse_exceptions.append({"path": relative, **waiver})
                    else:
                        errors.append({"path": relative, "error": str(error)})
    # Singleton groups cannot contribute to a baseline or a finding.
    return {"groups": {k: {"tokens": sizes[k], "locations": [asdict(x) for x in v]}
                       for k, v in groups.items() if len(v) > 1},
            "errors": errors, "unsupported": sorted(unsupported),
            "parse_exceptions": parse_exceptions,
            "files": files, "bytes": total_bytes}


def scope(group):
    return dict(Counter(location["path"] for location in group["locations"]))


def findings(current, base):
    result = {}
    for key, group in current["groups"].items():
        now = scope(group)
        old = scope(base["groups"].get(key, {"locations": []}))
        # Counts ignore line shifts and renames, but detect more files and more
        # occurrences within an already qualifying group.
        if len(now) >= 3 and (len(now) > len(old) or
                             sum(now.values()) > sum(old.values())):
            result[key] = group
    return result


def apply_exceptions(found, current, exceptions):
    errors = []
    seen = set()
    for entry in exceptions:
        key = entry.get("fingerprint")
        allowed = entry.get("files", {})
        if (key in seen or not isinstance(entry.get("rationale"), str)
                or not entry["rationale"].strip() or not allowed
                or any(not isinstance(n, int) or isinstance(n, bool) or n < 1
                       or any(c in p for c in "*?[]") for p, n in allowed.items())):
            errors.append({"exception": key, "error": "invalid or duplicate scoped exception"})
            continue
        seen.add(key)
        actual = scope(current["groups"].get(key, {"locations": []}))
        if actual != allowed:
            errors.append({"exception": key, "error": "exception scope changed or became stale"})
        else:
            found.pop(key, None)
    return errors


def baseline_policy(base_root, config):
    # Retain reviewed parse waivers from the base when the head fixes a grammar
    # failure and removes its now-stale waiver. Detection policy stays identical.
    previous = base_root / "tools/quality/duplicates.toml"
    waivers = dict(config.get("parse_exceptions", {}))
    if previous.exists():
        waivers.update(tomllib.loads(previous.read_text()).get("parse_exceptions", {}))
    return {**config, "parse_exceptions": waivers}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--baseline-root", type=Path, required=True,
                        help="separate checkout of intended base revision")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("duplicates.toml"))
    args = parser.parse_args()
    config = tomllib.loads(args.config.read_text())
    if config["min_tokens"] < 1:
        parser.error("min_tokens must be positive")
    root, base_root = args.root.resolve(), args.baseline_root.resolve()
    if not root.is_dir() or not base_root.is_dir() or root == base_root:
        parser.error("root and baseline-root must be distinct existing directories")
    started = time.monotonic()
    # Use the SAME policy for both revisions, including the first gate run.
    base = scan(base_root, baseline_policy(base_root, config))
    current = scan(root, config)
    found = findings(current, base)
    exception_errors = apply_exceptions(found, current, config.get("exceptions", []))
    used = {entry["path"] for entry in current["parse_exceptions"]}
    for path in config.get("parse_exceptions", {}):
        if path not in used:
            exception_errors.append({"path": path, "error": "stale parse exception"})
    for key, group in sorted(found.items()):
        print(f"Duplicate {key} ({group['tokens']} tokens):", file=sys.stderr)
        for loc in group["locations"]:
            print(f"  {loc['path']}:{loc['start']}-{loc['end']}", file=sys.stderr)
        loc = group["locations"][0]
        context = (root / loc["path"]).read_bytes()[loc["start_byte"]:loc["end_byte"]]
        group["context"] = context.decode("utf-8", errors="replace")
        print(group["context"][:1200], file=sys.stderr)
        print("Extract an appropriate abstraction or add a scoped exception with rationale.",
              file=sys.stderr)
    errors = [{"revision": revision, **error}
              for revision, report in (("base", base), ("current", current))
              for error in report["errors"]] + exception_errors
    for error in errors:
        print(json.dumps(error), file=sys.stderr)
    print(json.dumps({"detector": "exact-tree-v1", "findings": found, "errors": errors,
                      "current": current, "base": base,
                      "seconds": time.monotonic() - started}, sort_keys=True))
    return 2 if errors else int(bool(found))


if __name__ == "__main__":
    sys.exit(main())
