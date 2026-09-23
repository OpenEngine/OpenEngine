"""Exercise an extracted release offline, with no development tools on PATH."""
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request


def probe():
    # This code runs with -I under the installed interpreter, outside the checkout.
    from importlib.metadata import distributions
    from engine.apps.web import __main__
    from engine.apps.web.__main__ import read_configuration
    assert Path(__main__.__file__).is_relative_to(Path(sys.prefix))
    from migrations.migration import upgrade
    import sqlite3

    loaded, catalog = read_configuration()
    assert loaded.path is None
    assert len(catalog) > 0
    from platformdirs import user_config_path
    user_config = user_config_path("openengine") / "engine.toml"
    user_config.parent.mkdir(parents=True, exist_ok=True)
    user_config.write_text("show_projects = false\n")
    assert read_configuration()[0].config.show_projects is False
    user_config.unlink()
    for distribution in distributions():
        direct = distribution.read_text("direct_url.json")
        assert not direct or not json.loads(direct).get("dir_info", {}).get("editable")
    for store, revision in (("state", "sqlite_0008"), ("graph", "c7c9f42f4747")):
        path = Path.cwd() / f"old-{store}.sqlite3"
        url = f"sqlite:///{path}"
        upgrade(url, revision, store=store)
        with sqlite3.connect(path) as db:
            before = db.execute("SELECT version_num FROM alembic_version").fetchone()
            if store == "state":
                db.execute("INSERT INTO run_states (run_id, state_json) VALUES (?, ?)",
                           ("fixture", json.dumps({"run_id": "fixture", "origin": {"channel": "test", "thread_id": "1", "author": "test"}})))
        upgrade(url, store=store)
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT version_num FROM alembic_version").fetchone() != before
            if store == "state":
                assert db.execute("SELECT origin_channel FROM run_states WHERE run_id = 'fixture'").fetchone() == ("test",)
    custom = Path.cwd() / "custom"
    custom.mkdir()
    (custom / "workflow.py").write_text('from engine.apps.web.workflows.implementation_review_graph import workflow\n')
    config = Path.cwd() / "override.toml"
    config.write_text('show_projects = false\n[workflows]\ndirectory = "custom"\n')
    explicit, custom_catalog = read_configuration(config)
    assert explicit.config.show_projects is False
    assert explicit.workflows_directory == custom
    assert len(custom_catalog) == len(catalog)
    os.environ["ENGINE_CONFIG"] = str(config)
    assert read_configuration()[0].path == config


def main():
    bundle = Path(sys.argv[1]).resolve()
    report = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else None
    with tempfile.TemporaryDirectory(prefix="openengine-smoke-") as temporary:
        root = Path(temporary)
        clean_bin = root / "system-bin"
        clean_bin.mkdir()
        # Shell utilities are the only host executables exposed to installation.
        for name in ("sh", "dirname", "uname", "mkdir", "cp", "tar", "gzip", "shasum", "sha256sum"):
            found = shutil.which(name)
            if found:
                (clean_bin / name).symlink_to(found)
        home = root / "home"
        home.mkdir()
        env = {
            "HOME": str(home), "PATH": str(clean_bin), "LANG": "en_US.UTF-8",
            "UV_CACHE_DIR": str(root / "cache"), "UV_OFFLINE": "1",
        }
        prefix = root / "installed release"
        first, second = root / "first", root / "second"
        first.mkdir()
        second.mkdir()
        # A malicious/unrelated cwd config must not affect portable startup.
        (first / "engine.toml").write_text('invalid = true\n')
        (first / ".env").write_text("ENGINE_SERVICE_TOKEN=invalid\n")
        requirements = bundle / "requirements.txt"
        original = requirements.read_bytes()
        try:
            requirements.write_bytes(original + b"# corrupt artifact\n")
            rejected = subprocess.run(["/bin/sh", str(bundle / "install.sh"), str(prefix)], env=env, cwd=first, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            assert rejected.returncode != 0 and not prefix.exists()
        finally:
            requirements.write_bytes(original)
        subprocess.run(["/bin/sh", str(bundle / "install.sh"), str(prefix)], env=env, cwd=first, check=True)
        python = prefix / "venv/bin/python"
        subprocess.run([str(python), "-I", str(Path(__file__).resolve()), "--probe"], env=env, cwd=first, check=True)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            env["ENGINE_PORT"] = str(sock.getsockname()[1])
        base = f"http://127.0.0.1:{env['ENGINE_PORT']}"
        def request(path, body=None):
            data = None if body is None else json.dumps(body).encode()
            req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=2) as response:
                return response.read()
        thread = None
        for cwd in (first, second):
            with (root / "server.log").open("w+") as log:
                process = subprocess.Popen([str(prefix / "bin/engine-web")], env=env, cwd=cwd, stdout=log, stderr=log)
                try:
                    for _ in range(100):
                        try:
                            config = json.loads(request("/api/config"))
                            break
                        except (OSError, ValueError):
                            if process.poll() is not None:
                                log.seek(0)
                                raise AssertionError(log.read())
                            time.sleep(.1)
                    else:
                        log.seek(0)
                        raise AssertionError("Server did not start: " + log.read())
                    assert config["workflows"], config
                    html = request("/").decode()
                    assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', html)
                    assert assets
                    for asset in assets:
                        assert request(asset)
                    if thread is None:
                        thread = json.loads(request("/api/threads", {"agentId": config["defaultAgent"], "runner": "codex"}))
                        for runner in ("codex", "claude"):
                            result = request(f"/api/threads/{thread['id']}/runs", {"text": "hello", "runner": runner}).decode()
                            assert '"type": "error"' in result or '"type":"error"' in result, result
                            assert runner in result.lower(), result
                    else:
                        threads = json.loads(request("/api/threads"))["threads"]
                        assert thread["id"] in [item["id"] for item in threads]
                    assert not (cwd / "conversations.sqlite3").exists()
                except Exception:
                    time.sleep(.1)
                    log.seek(0)
                    print(log.read(), file=sys.stderr)
                    raise
                finally:
                    process.terminate()
                    process.wait(timeout=15)
        data = home / ("Library/Application Support/openengine" if sys.platform == "darwin" else ".local/share/openengine")
        assert (data / "conversations.sqlite3").exists()
        if report:
            manifest = json.loads((bundle / "manifest.json").read_text())
            report.write_text(json.dumps({"version": manifest["version"], "source_commit": manifest["source_commit"], "platform": manifest["platform"], "offline_install_and_startup": "passed", "restart_persistence": "passed", "packaged_migrations_and_overrides": "passed"}, indent=2) + "\n")
        print("Release smoke passed: offline install, UI/API, workflow, migrations, overrides, restart persistence")


if __name__ == "__main__":
    probe() if sys.argv[1] == "--probe" else main()
