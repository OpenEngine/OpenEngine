"""Artifact acceptance test; stdlib only, run with the bundled interpreter.

CI moves the entire checkout out of the way before running this file.
The install/server PATH excludes Python, Node, uv, and provider CLIs.
"""
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

bundle = Path(sys.argv[1]).resolve()
with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
base_url = f"http://127.0.0.1:{port}"
with tempfile.TemporaryDirectory(prefix="openengine-smoke-") as temporary:
    root = Path(temporary)
    home = root / "home"
    home.mkdir()
    # Only POSIX installer utilities, no ambient developer tools or credentials.
    utilities = root / "utilities"
    utilities.mkdir()
    for name in ("sh", "dirname", "sha256sum", "shasum", "uname", "mkdir", "cp", "ln"):
        if found := shutil.which(name):
            (utilities / name).symlink_to(found)
    env = {"HOME": str(home), "PATH": str(utilities),
           "ENGINE_CONFIG_DIR": str(home / "config"), "ENGINE_DATA_DIR": str(home / "data"),
           "ENGINE_LOG_DIR": str(home / "logs"), "UV_CACHE_DIR": str(home / "cache"),
           "ENGINE_INSTALL_DIR": str(home / "release"), "ENGINE_BIN_DIR": str(home / "bin")}
    first, second = root / "first", root / "second"
    first.mkdir()
    second.mkdir()
    (first / "engine.toml").write_text('[workflows]\ndirectory = "missing"\n')
    (home / "config").mkdir()
    (home / "config/engine.toml").write_text("show_projects = false\n")
    subprocess.run(["/bin/sh", str(bundle / "install.sh")], env=env, cwd=first, check=True)
    python = home / "release/venv/bin/python"
    # Both migration histories are loaded from wheels. Seed the previous state
    # schema with a real row, then let application startup upgrade it.
    subprocess.run([str(python), "-I", "-B", "-c", '''
import json, sqlite3, os
from pathlib import Path
from migrations.migration import upgrade
path = Path(os.environ["ENGINE_DATA_DIR"])
path.mkdir()
db = path / "conversations.sqlite3"
upgrade(f"sqlite:///{db}", "sqlite_0008")
with sqlite3.connect(db) as conn:
    conn.execute("INSERT INTO run_states (run_id, state_json) VALUES (?, ?)",
        ("fixture", json.dumps({"run_id":"fixture", "origin":{"channel":"C1", "thread_id":"17.5"}})))
upgrade(f"sqlite:///{db}")
with sqlite3.connect(db) as conn:
    assert conn.execute("SELECT origin_channel, origin_thread_id FROM run_states WHERE run_id='fixture'").fetchone() == ("C1", "17.5")
    conn.execute("DELETE FROM run_states WHERE run_id='fixture'")
upgrade(f"sqlite:///{path / 'fresh.sqlite3'}")
upgrade(f"sqlite:///{path / 'old-graph.sqlite3'}", "sqlite_graph_0001", store="graph")
upgrade(f"sqlite:///{path / 'old-graph.sqlite3'}", store="graph")
'''], env=env, cwd=first, check=True)

    def get(path):
        with urllib.request.urlopen(base_url + path, timeout=2) as response:
            return response.read()

    for cwd in (first, second):
        with (root / "server.log").open("w") as log:
            process = subprocess.Popen([str(home / "bin/engine-web"), "--port", str(port)], cwd=cwd, env=env, stdout=log, stderr=log)
            try:
                for _ in range(60):
                    try:
                        config = json.loads(get("/api/config"))
                        break
                    except (OSError, ValueError):
                        if process.poll() is not None:
                            raise AssertionError((root / "server.log").read_text())
                        time.sleep(.5)
                else:
                    raise AssertionError((root / "server.log").read_text())
                assert config["showProjects"] is False
                html = get("/").decode()
                for asset in re.findall(r'(?:src|href)="(/assets/[^\"]+)"', html):
                    assert get(asset)
                assert '/assets/' in html
                assert 'implementation-review' in json.dumps(config), config
                assert not any(cwd.glob("*.sqlite3"))
                # A user-created conversation must survive the next cwd/start.
                if cwd == first:
                    request = urllib.request.Request(base_url + "/api/threads", data=json.dumps({"agentId": "foreman", "runner": "codex"}).encode(), headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(request, timeout=10) as response:
                        created = json.load(response)
                    for runner in ("codex", "claude"):
                        request = urllib.request.Request(base_url + f'/api/threads/{created["id"]}/runs',
                            data=json.dumps({"text": "hello", "runner": runner}).encode(),
                            headers={"Content-Type": "application/json"})
                        with urllib.request.urlopen(request, timeout=10) as response:
                            failure = response.read().decode()
                        assert "not on PATH" in failure, failure
                else:
                    assert created["id"] in get("/api/threads").decode()
            except Exception:
                print((root / "server.log").read_text(), file=sys.stderr)
                raise
            finally:
                process.terminate()
                process.wait(timeout=15)
    # Explicit configuration and a custom workflow directory still take priority.
    subprocess.run([str(python), "-I", "-B", "-c", '''
import os, shutil
from pathlib import Path
from importlib.resources import files
from engine.apps.web.__main__ import read_configuration
root = Path(os.environ["HOME"]).resolve()
shutil.copytree(str(files("engine.apps.web").joinpath("default_workflows")), root / "custom")
config = root / "custom.toml"
config.write_text('[workflows]\\ndirectory = "custom"\\n[repos]\\nexample = "/custom/repository"\\n')
loaded, catalog = read_configuration(config)
assert loaded.workflows_directory == root / "custom"
assert loaded.config.repos["example"] == "/custom/repository"
assert catalog.graphs
os.environ["ENGINE_CONFIG"] = str(config)
assert read_configuration()[0].path == config
'''], env=env, cwd=second, check=True)
    assert (home / "logs/engine-web.log").is_file()
print("Artifact installation, assets/API, workflow, migrations, overrides and restart passed")
