"""Deterministic syntax clone detector v2. JSON on stdout; policy diagnostics on stderr.

No Git subprocesses: callers supply a separate checkout of the intended base.
"""

import argparse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import sys
import tomllib

from tree_sitter_language_pack import get_parser


LANGUAGES = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx",
    ".sh": "bash", ".rs": "rust", ".go": "go",
}
# Literal subtrees are atomic: whitespace and comment-like text inside them matters.
LITERALS = {"string", "string_literal", "template_string", "raw_string_literal", "interpreted_string_literal"}
PARSE_ERROR = "Tree-sitter parse error (file was not scanned)"


@dataclass(frozen=True)
class Location:
    path: str
    start: int
    end: int
    start_byte: int
    end_byte: int


def fingerprints(data, language, minimum, blinded=False):
    """Merkle hashes of exact subtrees; iterative, linear in syntax tree size."""
    tree = get_parser(language).parse(data)
    if tree.root_node.has_error:
        raise ValueError(PARSE_ERROR)
    stack = [(tree.root_node, False)]
    values = {}
    while stack:
        node, visited = stack.pop()
        if "comment" in node.type or (language == "python" and node.type == "line_continuation"):
            values[node.id] = (b"", 0)
            continue
        atomic = not node.children or node.type in LITERALS
        if not visited and not atomic:
            stack.append((node, True))
            stack.extend((child, False) for child in reversed(node.children))
            continue
        digest = hashlib.sha256()
        kind = node.type
        if blinded:
            if "identifier" in kind or kind == "variable_name":
                kind = "ID"
            elif kind in LITERALS or "string" in kind:
                kind = "STR"
            elif kind in {"integer", "float", "number", "integer_literal", "float_literal", "decimal_integer_literal"}:
                kind = "NUM"
        digest.update(kind.encode() + b"\0")
        count = 1
        if atomic:
            if not blinded or kind not in {"ID", "STR", "NUM"}:
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


UNIT_TYPES = {"function_definition", "function_declaration", "method_definition", "method_declaration",
              "class_definition", "class_declaration", "arrow_function", "function_item",
              "function_expression", "generator_function", "generator_function_declaration", "func_literal"}


def is_test(path, config):
    parts = path.lower().split("/")
    return any(fnmatch.fnmatchcase(part, pattern) for part in parts
               for pattern in config.get("test_patterns", []))


def fuzzy_groups(units, threshold):
    """Inverted-index candidates, exact Jaccard, deterministic complete-link groups.

    Every pair in a group passes, preventing A-B-C chains. Greedy partitioning
    is intentionally conservative and deterministic, rather than maximal cliques.
    """
    units = sorted(units, key=lambda u: (u["path"], u["start_byte"], u["exact"]))
    sets = [set(u["shingles"]) for u in units]
    index, edges = defaultdict(list), {}
    for i, unit in enumerate(units):
        candidates = set()
        for shingle in sorted(sets[i]):
            candidates.update(index[(unit["language"], shingle)])
        for j in sorted(candidates):
            union = sets[i] | sets[j]
            score = len(sets[i] & sets[j]) / len(union) if union else 0
            if score >= threshold:
                edges[j, i] = score
        for shingle in sorted(sets[i]):
            index[unit["language"], shingle].append(i)
    clusters, membership = [], {}
    neighbors = defaultdict(set)
    for a, b in edges:
        neighbors[b].add(a)
    for i in range(len(units)):
        choices = sorted({membership[j] for j in neighbors[i]})
        for choice in choices:
            cluster = clusters[choice]
            if all(j in neighbors[i] for j in cluster):
                cluster.append(i)
                membership[i] = choice
                break
        else:
            membership[i] = len(clusters)
            clusters.append([i])
    result = {}
    for cluster in clusters:
        if len({units[i]["path"] for i in cluster}) < 3:
            continue
        members = []
        for i in cluster:
            peers = [j for j in cluster if j != i]
            reference = min(peers, key=lambda j: (edges[min(i, j), max(i, j)], j))
            members.append({**units[i], "score": edges[min(i, reference), max(i, reference)],
                            "reference": {"path": units[reference]["path"], "start": units[reference]["start"]},
                            "added_shingles": sorted(sets[i] - sets[reference]),
                            "removed_shingles": sorted(sets[reference] - sets[i]),
                            "differing_statements": {
                                "member": [units[i].get("shingle_context", {}).get(h, h)
                                           for h in sorted(sets[i] - sets[reference])],
                                "reference": [units[reference].get("shingle_context", {}).get(h, h)
                                              for h in sorted(sets[reference] - sets[i])] }})
        # Reviewable exception identity uses blinded unit identities and exact paths/counts.
        identity = sorted((m["path"], m["blinded"]) for m in members)
        key = "type3:" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        result[key] = {"kind": "type3", "tokens": min(m["tokens"] for m in members), "locations": members}
    return result


def fuzzy_findings(current, base):
    old = Counter((u["language"], u["exact"]) for u in base.get("units", []))
    now = Counter((u["language"], u["exact"]) for u in current.get("units", []))
    groups = current.get("fuzzy_groups")
    if groups is None:
        groups = fuzzy_groups(current.get("units", []), 0.8)
    result = {}
    for key, group in groups.items():
        identities = {(u["language"], u["exact"]) for u in group["locations"]}
        old_paths = {u["path"] for u in base.get("units", [])
                     if (u["language"], u["exact"]) in identities}
        if (len(scope(group)) > len(old_paths)
                or any(now[identity] > old[identity] for identity in identities)):
            result[key] = group
    return result


def prune_nested(groups):
    kept = {}
    # Largest first; exact wins equal spans, then type2, then type3.
    ordered = sorted(groups.items(), key=lambda item: (
        -sum(loc["end_byte"] - loc["start_byte"] for loc in item[1]["locations"]),
        item[1].get("kind", "exact"), item[0]))
    for key, group in ordered:
        spread = set(scope(group))
        if any(set(scope(larger)) == spread and all(
                any(a["path"] == b["path"] and a["start_byte"] >= b["start_byte"]
                    and a["end_byte"] <= b["end_byte"] for b in larger["locations"])
                for a in group["locations"]) for larger in kept.values()):
            continue
        kept[key] = group
    return kept


def scan(root, config):
    groups = defaultdict(list)
    units, excluded_tests = [], []
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
                if path.is_symlink():
                    continue
                if is_test(relative, config):
                    excluded_tests.append(relative)
                    continue
                if any(relative == p or relative.startswith(p + "/") for p in excluded_paths):
                    continue
                language = LANGUAGES.get(path.suffix.lower())
                if language is None:
                    unsupported.append(relative)
                    continue
                try:
                    data = path.read_bytes()
                    exact = list(fingerprints(data, language, min(config["min_tokens"], config.get("unit_min_tokens", config["min_tokens"]))))
                    inner = list(fingerprints(data, language, 5, True))
                    blind = [(h, size, n) for h, size, n in inner if size >= config["min_tokens"]]
                    files += 1
                    total_bytes += len(data)
                    for channel, entries in (("exact", exact), ("type2", blind)):
                        for key, size, node in entries:
                            # Module wrappers are containers, not reusable structures.
                            if node.parent is None or size < config["min_tokens"]:
                                continue
                            key = language + ":" + ("type2:" if channel == "type2" else "") + key
                            sizes[key] = size
                            groups[key].append(Location(relative, node.start_point.row + 1,
                                                        node.end_point.row + 1,
                                                        node.start_byte, node.end_byte))
                    exact_nodes = {(n.start_byte, n.end_byte, n.type): key for key, _, n in exact}
                    for key, size, node in inner:
                        if node.type not in UNIT_TYPES or size < config.get("unit_min_tokens", config["min_tokens"]):
                            continue
                        # Nontrivial inner subtrees preserve operators and structure.
                        contexts = {h: data[child.start_byte:min(child.end_byte, child.start_byte + 160)].decode("utf-8", errors="replace")
                                    for h, _, child in inner
                                    if node.start_byte <= child.start_byte and child.end_byte <= node.end_byte
                                    and child != node and child.type not in UNIT_TYPES}
                        shingles = sorted({h for h, _, child in inner
                                           if node.start_byte <= child.start_byte
                                           and child.end_byte <= node.end_byte and child != node
                                           and child.type not in UNIT_TYPES})
                        units.append({"language": language, "exact": exact_nodes[node.start_byte, node.end_byte, node.type],
                                      "blinded": key, "tokens": size, "shingles": shingles, "shingle_context": contexts,
                                      **asdict(Location(relative, node.start_point.row + 1,
                                                       node.end_point.row + 1, node.start_byte, node.end_byte))})
                except (OSError, ValueError) as error:
                    waiver = config.get("parse_exceptions", {}).get(relative, {})
                    if (isinstance(error, ValueError) and waiver.get("rationale", "").strip()
                            and waiver.get("sha256") == hashlib.sha256(data).hexdigest()):
                        parse_exceptions.append({"path": relative, **waiver})
                    else:
                        errors.append({"path": relative, "error": str(error)})
    # Singleton groups cannot contribute to a baseline or a finding.
    return {"groups": {k: {"kind": "type2" if ":type2:" in k else "exact", "tokens": sizes[k], "locations": [asdict(x) for x in v]}
                       for k, v in groups.items() if len(v) > 1},
            "units": units, "excluded_tests": sorted(excluded_tests),
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
    result.update(fuzzy_findings(current, base))
    return prune_nested(result)


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
        actual = scope(current["groups"].get(key, current.get("fuzzy_groups", {}).get(key, {"locations": []})))
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
    if (config["min_tokens"] < 1 or config.get("unit_min_tokens", 100) < 1
            or not 0 < config.get("similarity_threshold", .8) <= 1):
        parser.error("token minima must be positive and similarity_threshold in (0, 1]")
    root, base_root = args.root.resolve(), args.baseline_root.resolve()
    if not root.is_dir() or not base_root.is_dir() or root == base_root:
        parser.error("root and baseline-root must be distinct existing directories")
    # Use the SAME policy for both revisions, including the first gate run.
    base = scan(base_root, baseline_policy(base_root, config))
    current = scan(root, config)
    current["fuzzy_groups"] = fuzzy_groups(current["units"], config.get("similarity_threshold", 0.8))
    base["fuzzy_groups"] = fuzzy_groups(base["units"], config.get("similarity_threshold", 0.8))
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
            if "score" in loc:
                print(f"    similarity={loc['score']:.6f} reference={loc['reference']}", file=sys.stderr)
                print(json.dumps(loc["differing_statements"], sort_keys=True), file=sys.stderr)
        loc = group["locations"][0]
        with (root / loc["path"]).open("rb") as source:
            source.seek(loc["start_byte"])
            context = source.read(min(1200, loc["end_byte"] - loc["start_byte"]))
        group["context"] = context.decode("utf-8", errors="replace")
        print(group["context"][:1200], file=sys.stderr)
        print("Extract an appropriate abstraction or add a scoped exception with rationale.",
              file=sys.stderr)
    errors = [{"revision": revision, **error}
              for revision, report in (("base", base), ("current", current))
              for error in report["errors"]] + exception_errors
    for error in errors:
        print(json.dumps(error), file=sys.stderr)
    print(json.dumps({"detector": "tree-clones-v2", "findings": found, "errors": errors,
                      "current": current, "base": base}, sort_keys=True))
    return 2 if errors else int(bool(found))


if __name__ == "__main__":
    sys.exit(main())
