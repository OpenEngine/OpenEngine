"""Artifact contract, run by the installed interpreter with no source checkout."""
import asyncio
import json
import os
from pathlib import Path
import re
import sqlite3
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen

from engine.apps.web.__main__ import read_configuration, _settings
from engine.apps.web.paths import config_directory, data_directory, log_directory
from engine.apps.web.composition import build_runners
from engine.domain.ids import AgentRunId
from engine.runtime.profiles import FOREMAN
from migrations.migration import upgrade
from alembic.script import ScriptDirectory
from migrations.migration import alembic_config


PORT = 8000


def request(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    with urlopen(Request(f"http://127.0.0.1:{PORT}" + path, data=data,
                         headers={"Content-Type": "application/json"}), timeout=3) as response:
        return response.read()


def main():
    assert not any(__import__("shutil").which(name) for name in ("python", "python3", "node", "npm", "codex", "claude"))
    global PORT
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        PORT = listener.getsockname()[1]
    loaded, catalog = read_configuration()
    assert loaded.path is None
    assert catalog and len(catalog.graphs) > 0
    assert _settings(loaded).host == "127.0.0.1"
    for runner in build_runners(_settings(loaded)).values():
        try:
            asyncio.run(runner.run_turn(AgentRunId("missing-cli"), FOREMAN, []))
        except RuntimeError as error:
            assert "not on PATH" in str(error), error
        else:
            raise AssertionError("Missing agent CLI was not reported")
    user_config = config_directory() / "engine.toml"
    user_config.write_text("show_projects = false\n")
    try:
        assert read_configuration()[0].config.show_projects is False
    finally:
        user_config.unlink()
    # Migrate a real populated prior revision, for both Engine-owned histories.
    with tempfile.TemporaryDirectory() as temporary:
        temp = Path(temporary)
        for store in ("state", "graph"):
            database = temp / f"{store}.sqlite3"
            url = f"sqlite:///{database}"
            scripts = ScriptDirectory.from_config(alembic_config(url, store=store))
            first = list(scripts.walk_revisions())[-1].revision
            upgrade(url, first, store=store)
            with sqlite3.connect(database) as connection:
                if store == "state":
                    connection.execute("INSERT INTO projects (project_id, name) VALUES ('fixture', 'preserved')")
                else:
                    connection.execute("INSERT INTO runs (run_id, graph_id) VALUES ('fixture', 'preserved')")
            upgrade(url, store=store)
            with sqlite3.connect(database) as connection:
                query = "SELECT name FROM projects" if store == "state" else "SELECT graph_id FROM runs"
                assert connection.execute(query).fetchone() == ("preserved",)
                assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == scripts.get_current_head()
        # Relative explicit config and custom workflows are resolved beside config.
        custom = temp / "custom"
        custom.mkdir()
        (custom / "example.py").write_text('from engine.graph_runtime_langgraph import graph_workflow, State\nfrom langgraph.graph import StateGraph, START, END\ng = StateGraph(State)\ng.add_edge(START, END)\nworkflow = graph_workflow(g, id="custom", name="Custom")\n')
        config = temp / "custom.toml"
        config.write_text('show_projects = false\n[workflows]\ndirectory = "custom"\n')
        original_cwd = Path.cwd()
        try:
            os.chdir(temp)
            override, workflows = read_configuration("custom.toml")
        finally:
            os.chdir(original_cwd)
        assert not override.config.show_projects and workflows.get("custom")
        os.environ["ENGINE_CONFIG"] = str(config)
        assert read_configuration()[0].path == config.resolve()
        del os.environ["ENGINE_CONFIG"]
        thread_id = None
        for index in range(2):
            cwd = temp / str(index)
            cwd.mkdir()
            # Ambient deployment files must not affect the portable release.
            (cwd / "engine.toml").write_text('public_url = "https://unwanted.invalid"\n')
            with (temp / f"server-{index}.log").open("w+") as log:
                server = subprocess.Popen([str(Path(sys.executable).parent / "engine-web"), "--port", str(PORT)], cwd=cwd,
                                          stdout=log, stderr=subprocess.STDOUT)
                try:
                    for _ in range(100):
                        try:
                            config_json = json.loads(request("/api/config"))
                            break
                        except OSError:
                            if server.poll() is not None:
                                log.seek(0)
                                raise AssertionError(log.read())
                            time.sleep(0.1)
                    else:
                        raise AssertionError("server did not start")
                    assert config_json["workflows"], config_json
                    html = request("/").decode()
                    assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', html)
                    assert assets
                    for asset in assets:
                        assert request(asset)
                    if thread_id is None:
                        thread = json.loads(request("/api/threads", {
                            "agentId": config_json["defaultAgent"], "runner": "codex"}))
                        thread_id = thread["id"]
                    else:
                        assert json.loads(request(f"/api/threads/{thread_id}"))["id"] == thread_id
                    assert not (cwd / "conversations.sqlite3").exists()
                    assert (data_directory() / "conversations.sqlite3").is_file()
                except BaseException:
                    time.sleep(0.2)
                    log.flush()
                    log.seek(0)
                    print(log.read(), file=sys.stderr)
                    raise
                finally:
                    server.terminate()
                    server.wait(timeout=15)
    assert (log_directory() / "engine-web.log").stat().st_size > 0
    print("Release artifact smoke tests passed")


if __name__ == "__main__":
    main()
