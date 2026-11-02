"""Repository-owned deterministic checks. No network, model or forge dependency."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import tomllib


def _invalid(message: str) -> None:
    raise ValueError(message)


def run_checks(root: Path) -> dict:
    """Run every required scan; retain full output outside model context."""
    reports = root / ".engine" / "quality"
    reports.mkdir(parents=True, exist_ok=True)
    report = Path(tempfile.mkdtemp(dir=reports))
    diagnostics = []
    try:
        config = tomllib.loads((root / ".quality.toml").read_text())
        if set(config) != {"version", "checks"}:
            _invalid("configuration must contain only version and checks")
        checks = config["checks"]
        if config.get("version") != 1 or not isinstance(checks, list) or not checks:
            _invalid("version must be 1 and checks must be nonempty")
        ids = set()
        for check in checks:
            if not isinstance(check, dict) or set(check) - {"id", "command", "files", "timeout"}:
                _invalid("unknown check fields; expected id, command, files, timeout")
            timeout = check.get("timeout", 600)
            if not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
                _invalid("timeout must be positive and at most 3600 seconds")
            name, command, scans = check["id"], check["command"], check["files"]
            if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_-]+", name) or name in ids:
                _invalid("check IDs must be unique lowercase identifiers")
            ids.add(name)
            if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
                _invalid(f"{name}: command must be a nonempty argv array")
            if not isinstance(scans, list) or not scans or not all(isinstance(x, str) and x for x in scans):
                _invalid(f"{name}: files must contain required scan globs")
            reproduce = shlex.join(command)
            missing = [pattern for pattern in scans if not any(p.is_file() for p in root.glob(pattern))]
            if missing:
                diagnostics.append(dict(id=name, file=".quality.toml", line=1,
                    message=f"Required scan matched no files: {missing}", command=reproduce))
                continue
            try:
                with (report / f"{name}.log").open("w") as output:
                    result = subprocess.run(command, cwd=root, stdout=output, stderr=subprocess.STDOUT,
                                            timeout=timeout, check=False)
                if result.returncode:
                    log = (report / f"{name}.log").read_text(errors="replace")
                    failed_tests = list(re.finditer(r"^FAILED ([^:\s]+)::([^\n]+)", log, re.MULTILINE))[:10]
                    for failure in failed_tests:
                        location = re.search(re.escape(failure[1]) + r":(\d+):", log)
                        diagnostics.append(dict(id=name, file=failure[1], line=int(location[1]) if location else 1,
                            message=f"Fix failing test {failure[2]}"[:400], command=reproduce))
                    if failed_tests:
                        continue
                    # Common compiler/linter format. Non-location failures still identify the config.
                    matches = list(re.finditer(r"([^\s:]+):(\d+)(?::\d+)?:?\s*([^\n]+)", log))[:10]
                    for match in matches:
                        diagnostics.append(dict(id=name, file=match[1], line=int(match[2]),
                                                message=match[3][:400], command=reproduce))
                    if not matches:
                        diagnostics.append(dict(id=name, file=".quality.toml", line=1,
                            message=f"Exit {result.returncode}; inspect {name}.log and fix the check failure.",
                            command=reproduce))
            except (OSError, subprocess.TimeoutExpired) as error:
                diagnostics.append(dict(id=name, file=".quality.toml", line=1,
                    message=f"Unable to execute required check: {error}"[:400], command=reproduce))
    except (OSError, ValueError, KeyError, TypeError) as error:
        diagnostics.append(dict(id="configuration", file=".quality.toml", line=1,
            message=f"Invalid required quality configuration: {error}"[:400],
            command="python scripts/quality.py"))
    result = dict(passed=not diagnostics, diagnostics=diagnostics[:20], report=str(report))
    (report / "result.json").write_text(json.dumps({**result, "diagnostics": diagnostics}, indent=2))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    result = run_checks(parser.parse_args().root.resolve())
    print(json.dumps(result))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()

