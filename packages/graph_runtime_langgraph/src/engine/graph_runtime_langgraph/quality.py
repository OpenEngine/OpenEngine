"""Repository-owned deterministic checks. No network, model or forge dependency."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import shutil
import signal
import sys
import threading
import time
import tempfile
import tomllib


def _invalid(message: str) -> None:
    raise ValueError(message)


def _execution(command: list[str], root: Path, temporary: Path) -> tuple[list[str], dict[str, str]]:
    """Deny host writes, credentials and networking; never fall back to host execution."""
    environment = {"PATH": str(Path(sys.prefix) / "bin") + os.pathsep + os.defpath, "HOME": str(temporary), "TMPDIR": str(temporary),
                   "LANG": "C.UTF-8", "UV_OFFLINE": "1", "UV_NO_SYNC": "1",
                   "UV_CACHE_DIR": str(temporary / "uv"), "UV_PYTHON": sys.executable}
    # Installed interpreters and tools are explicit read-only inputs, not the host home.
    executable = shutil.which(command[0])
    if executable:
        command = [executable, *command[1:]]
    roots = {Path(p).resolve() for p in (sys.prefix, sys.base_prefix)}
    roots.update(Path(p) for p in ("/usr", "/bin", "/lib", "/lib64", "/nix/store") if Path(p).exists())
    # Pyright needs the preinstalled Node runtime even when setup-node put it
    # outside system directories. Expose the binary, not the host tool cache.
    node = shutil.which("node")
    if node:
        environment["PATH"] += os.pathsep + str(Path(node).parent)
    boundary = shutil.which("bwrap", path=os.defpath)
    if sys.platform != "linux" or not boundary:
        message = "Required quality sandbox unavailable: Linux with bubblewrap is required"
        raise OSError(message)
    argv = [boundary, "--unshare-all", "--die-with-parent", "--new-session", "--proc", "/proc", "--dev", "/dev"]
    for path in sorted(roots):
        argv += ["--ro-bind", str(path), str(path)]
    for filename in ("passwd", "group", "nsswitch.conf", "hosts", "localtime", "ld.so.cache"):
        path = Path("/etc") / filename
        if path.exists():
            argv += ["--ro-bind", str(path.resolve()), str(path)]
    for tool in dict.fromkeys(path for path in (executable, node) if path):
        if not any(Path(tool).is_relative_to(path) for path in roots | {root}):
            argv += ["--ro-bind", str(Path(tool).resolve()), tool]
    argv += ["--bind", str(root), str(root), "--bind", str(temporary), str(temporary),
             # Keep host-managed artifacts immutable to the untrusted check. Output
             # still flows through the already-open stdout descriptor.
             "--ro-bind", str(root / ".engine"), str(root / ".engine"),
             "--chdir", str(root), "--", *command]
    return argv, environment


def _execute(command, root, output, timeout, cancelled):
    with tempfile.TemporaryDirectory(prefix="engine-quality-") as directory:
        argv, environment = _execution(command, root, Path(directory).resolve())
        with subprocess.Popen(argv, cwd=root, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                              env=environment, start_new_session=True) as process:
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    if cancelled.wait(0.05):
                        raise InterruptedError()
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(command, timeout)
                return process.returncode
            finally:
                # Kill descendants even if the immediate command already exited.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()


def _diagnostics(log: Path, name: str, reproduce: str) -> list[dict]:
    locations = {}
    failures = []
    matches = []
    # Bounded reads also handle a multi-gigabyte single line without allocating it.
    with log.open(errors="replace") as stream:
        for _ in range(10000):
            line = stream.readline(4096)
            if not line:
                break
            location = re.match(r"^([^\s:]{1,1024}):(\d{1,9})(?::\d{1,9})?:?\s*(.*)", line)
            if location:
                locations.setdefault(location[1], int(location[2]))
                if len(matches) < 10:
                    matches.append(dict(id=name, file=location[1], line=int(location[2]),
                                        message=location[3][:400], command=reproduce))
            failure = re.match(r"^FAILED ([^:\s]{1,1024})::(.+)", line)
            if failure:
                failures.append(dict(id=name, file=failure[1], line=locations.get(failure[1], 1),
                                     message=f"Fix failing test {failure[2]}"[:400], command=reproduce))
                if len(failures) == 10:
                    break
            if len(matches) == 10:
                break
    return failures or matches


def run_checks(root: Path, cancelled: threading.Event | None = None) -> dict:
    """Run every required scan; retain full output outside model context."""
    root = root.resolve()
    cancelled = cancelled or threading.Event()
    reports = root / ".engine" / "quality"
    try:
        if (root / ".engine").is_symlink() or reports.is_symlink():
            _invalid("quality artifact directories must not be symlinks")
        reports.mkdir(parents=True, exist_ok=True)
        report = Path(tempfile.mkdtemp(dir=reports))
    except (OSError, ValueError) as error:
        # Do not follow unsafe paths or require working artifact storage to
        # return a repairable failure to the implementor.
        return dict(passed=False, report=str(reports), diagnostics=[dict(
            id="artifacts", file=".engine/quality", line=1,
            message=f"Cannot prepare quality reports: {error}. Replace conflicting files or symlinks with real directories and grant checkout write access. No checks ran and no report was saved.",
            command="python scripts/quality.py",
        )])
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
            if cancelled.is_set():
                raise InterruptedError()
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
                    message=f"Required scan matched no files: {missing}. Correct the files globs in .quality.toml or restore the required source files.", command=reproduce))
                continue
            try:
                with (report / f"{name}.log").open("w") as output:
                    returncode = _execute(command, root, output, timeout, cancelled)
                if returncode:
                    found = _diagnostics(report / f"{name}.log", name, reproduce)
                    diagnostics.extend(found or [dict(id=name, file=".quality.toml", line=1,
                        message=f"Check {name} exited with status {returncode}. Inspect {report / f'{name}.log'} for the failure details; fix the reported source or check configuration and rerun the command.",
                        command=reproduce)])
            except (OSError, subprocess.TimeoutExpired) as error:
                if isinstance(error, subprocess.TimeoutExpired):
                    action = f"Check exceeded its {timeout}s timeout. Fix a hung or slow check, or adjust its timeout in .quality.toml (maximum 3600s)."
                elif isinstance(error, FileNotFoundError):
                    action = f"Install the required executable {command[0]!r} or correct its command in .quality.toml; provision dependencies before disconnected execution."
                else:
                    action = "Verify the executable and checkout permissions and the Linux Bubblewrap prerequisites in docs/local-quality.md."
                diagnostics.append(dict(id=name, file=".quality.toml", line=1,
                    message=f"Unable to execute required check: {error}. {action}"[:800], command=reproduce))
    except (OSError, ValueError, KeyError, TypeError) as error:
        diagnostics.append(dict(id="configuration", file=".quality.toml", line=1,
            message=f"Invalid required quality configuration: {error}. Create or correct .quality.toml with version = 1 and nonempty [[checks]] entries containing unique id, command argv, and files globs."[:800],
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

