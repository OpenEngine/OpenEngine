"""Run with the installed Python, from an unrelated directory without the checkout."""
import json
from importlib import metadata
from importlib.resources import files
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

from alembic.script import ScriptDirectory
from engine.apps.web.__main__ import STATIC_DIRECTORY, read_configuration
from engine.apps.web.paths import data_directory, config_directory, log_directory
from migrations.migration import alembic_config, upgrade


def fetch(path):
    with urllib.request.urlopen("http://127.0.0.1:8000" + path, timeout=2) as response:
        return response.read()


def serve(directory):
    log = (directory / "server.log").open("w+")
    process = subprocess.Popen([str(Path(sys.executable).with_name("engine-web"))],
                               cwd=directory, stdout=log, stderr=log)
    try:
        for _ in range(120):
            if process.poll() is not None:
                raise RuntimeError("server exited")
            try:
                config = json.loads(fetch("/api/config"))
                assert config["workflows"], "bundled workflow did not compile"
                html = fetch("/").decode()
                assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', html)
                assert assets
                for asset in assets:
                    assert fetch(asset)
                return
            except (OSError, TimeoutError):
                time.sleep(0.25)
        raise RuntimeError("server did not become ready")
    finally:
        process.terminate()
        process.wait(timeout=15)
        log.seek(0)
        print(log.read())
        log.close()


def main():
    manifest = json.loads((Path(__file__).parent / "manifest.json").read_text())
    assert ".".join(map(str, sys.version_info[:3])) == manifest["python"]
    assert metadata.version("engine-web") == manifest["version"]
    for distribution in metadata.distributions():
        assert not distribution.read_text("direct_url.json"), "unexpected source install"
    assert not shutil.which("claude") and not shutil.which("codex")
    assert not shutil.which("node") and not shutil.which("python")
    assert STATIC_DIRECTORY.is_relative_to(Path(sys.prefix))
    loaded, catalog = read_configuration()
    assert loaded.path is None and catalog.graphs
    assert not loaded.config.repos and not loaded.config.public_url
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        # Both stores initialize from packaged histories and upgrade earlier revisions.
        for store, old in (("state", "sqlite_0008"), ("graph", "sqlite_graph_0001")):
            url = f"sqlite:///{root / (store + '.sqlite3')}"
            upgrade(url, old, store=store)
            if store == "state":
                with sqlite3.connect(root / "state.sqlite3") as db:
                    db.execute("INSERT INTO projects (project_id, name) VALUES ('fixture', 'preserved')")
            upgrade(url, store=store)
            with sqlite3.connect(root / (store + ".sqlite3")) as db:
                assert db.execute("SELECT version_num FROM alembic_version").fetchone()[0] == ScriptDirectory.from_config(alembic_config(url, store=store)).get_current_head()
                if store == "state":
                    assert db.execute("SELECT name FROM projects WHERE project_id='fixture'").fetchone() == ("preserved",)
        first, second = root / "first", root / "second"
        first.mkdir()
        second.mkdir()
        # An incidental checkout configuration must not override portable defaults.
        (first / "engine.toml").write_text("invalid TOML !")
        serve(first)
        database = data_directory() / "conversations.sqlite3"
        with sqlite3.connect(database) as db:
            db.execute("INSERT INTO projects (project_id, name) VALUES ('restart', 'retained')")
        serve(second)
        with sqlite3.connect(database) as db:
            assert db.execute("SELECT name FROM projects WHERE project_id='restart'").fetchone() == ("retained",)
        assert (log_directory() / "engine-web.log").exists()
        assert not (first / "conversations.sqlite3").exists()
        # Explicit and environment config, relative custom workflows, user config.
        custom = root / "custom"
        custom.mkdir()
        bundled = Path(str(files("engine.apps.web").joinpath("default_workflows")))
        shutil.copy2(bundled / "implementation_review_graph.py", custom / "workflow.py")
        config = root / "explicit.toml"
        config.write_text('show_projects = false\n[workflows]\ndirectory = "custom"\n')
        for mode in ("explicit", "environment", "user"):
            os.environ.pop("ENGINE_CONFIG", None)
            if mode == "environment":
                os.environ["ENGINE_CONFIG"] = str(config)
            elif mode == "user":
                config_directory().mkdir(parents=True, exist_ok=True)
                (config_directory() / "engine.toml").write_text('show_projects = false\n')
            selected, graphs = read_configuration(config if mode == "explicit" else None)
            assert not selected.config.show_projects and graphs.graphs
    print("Installed artifact smoke checks passed")


if __name__ == "__main__":
    main()
