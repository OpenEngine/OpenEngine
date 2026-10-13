"""Inspect and select the local sandbox backend without starting a VM."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

import tomlkit
from tomlkit.exceptions import TOMLKitError

from engine.adapters.sandbox.smolvm.support import detect_support
from engine.apps.cli import daemon
from engine.runtime.config import EngineConfigError, load_engine_config, parse_engine_config


def add_parser(commands: argparse._SubParsersAction) -> None:
    command = commands.add_parser("vm", help="show or select the local sandbox backend")
    command.add_argument("backend", nargs="?", choices=("process", "smolvm"), help="process is the default")
    command.add_argument("--image", help="local SmolVM guest archive; reuse the configured image when omitted")
    command.add_argument("--config", type=Path, help="configuration file (default: ENGINE_CONFIG or installed configuration)")
    command.add_argument("--json", action="store_true", help="print the configured backend as JSON")


def _save(path: Path, original: str, updated: str) -> None:
    """Replace the file atomically, retaining its permissions and TOML comments."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        if path.read_text(encoding="utf-8") != original:
            message = "configuration changed during selection; retry engine vm"
            raise ValueError(message)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _refuse(message: str) -> None:
    raise ValueError(message)


def _select(arguments: argparse.Namespace) -> dict:
    if arguments.image is not None and arguments.backend != "smolvm":
        _refuse("--image requires engine vm smolvm")
    path = (arguments.config or daemon.config_path()).expanduser().resolve()
    if not path.is_file():
        _refuse(f"{path} does not exist; run the installer or select an existing file with --config")
    sandbox = load_engine_config(path).config.sandbox
    changed = arguments.backend is not None
    if changed:
        original = path.read_text(encoding="utf-8")
        document = tomlkit.parse(original)
        image = sandbox.image
        if arguments.backend == "smolvm":
            image = arguments.image if arguments.image is not None else image
            if not image or not image.strip():
                _refuse("SmolVM requires --image /path/to/guest.tar or an existing sandbox.image")
            archive = Path(image).expanduser()
            archive = archive if archive.is_absolute() else (Path.cwd() if arguments.image is not None else path.parent) / archive
            archive = archive.resolve()
            if not archive.is_file() or archive.stat().st_size == 0:
                _refuse(f"guest image must be a nonempty local archive: {archive}")
            with archive.open("rb") as stream:
                stream.read(1)
            record = daemon.read_record()
            support = detect_support(path=record.spec.environment()["PATH"] if record else None)
            if not support.available:
                _refuse(support.reason)
            image = str(archive)
        if "sandbox" not in document:
            document["sandbox"] = tomlkit.table()
        table = document["sandbox"]
        table["backend"] = arguments.backend
        if arguments.backend == "smolvm":
            table["image"] = image
        updated = tomlkit.dumps(document)
        sandbox = parse_engine_config(document.unwrap()).sandbox
        _save(path, original, updated)
    notice = "Existing services must be restarted to load changes (engine daemon restart for the managed service). Node execution remains on the host until sandbox routing is implemented."
    return {"config": str(path), "backend": sandbox.backend, "image": sandbox.image, "changed": changed, "notice": notice}


def main(arguments: argparse.Namespace) -> int:
    try:
        result = _select(arguments)
    except (EngineConfigError, OSError, ValueError, TOMLKitError) as error:
        print(f"engine vm: {error}", file=sys.stderr)
        return 1
    if arguments.json:
        print(json.dumps(result))
    else:
        print(f"Sandbox backend: {result['backend']}\nConfig: {result['config']}")
        if result['image']:
            print(f"Guest image: {result['image']}")
        print(result['notice'])
    return 0
