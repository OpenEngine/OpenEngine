"""What the development server decides, without starting anything.

`engine-dev` supervises two watchers it did not write, so what is worth testing
is the configuration it hands them and the reasoning around it: which changes
restart the API, which must not, and what each child is told about the other.
The reload filter is Uvicorn's own class rather than a description of it -- a
test that asserted on our flags would pass while the flags meant something
else.

Neither tier is started here -- running them is what the acceptance criteria in
`docs/dev-server.md` are for. Shutdown is the exception, because "Ctrl-C leaves
nothing behind" is a claim about real processes and is only true if the ones
that ignore the polite signal are killed anyway.
"""

import importlib
import os
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from uvicorn.config import Config

from engine.apps.web import dev
from engine.runtime import CONFIG_ENVIRONMENT_VARIABLE

pytest.importorskip("watchfiles", reason="the reload filter under test is watchfiles'")
from uvicorn.supervisors.watchfilesreload import FileFilter  # noqa: E402


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A checkout with everything the exclusions are about, actually present.

    Built rather than borrowed: the reloader decides at startup whether an
    exclusion names a directory, so a test run against this repository would
    quietly assert something different depending on whether `npm install` had
    been run in it.
    """
    for directory in (
        ".git",
        ".venv/lib/python3.11/site-packages/starlette",
        "apps/web/dist/assets",
        "apps/web/node_modules/some-package",
        "apps/web/src/engine/apps/web",
        "packages/runtime/src/engine/runtime",
        "workflows",
    ):
        (tmp_path / directory).mkdir(parents=True)
    (tmp_path / "engine.toml").write_text("default_branch = 'main'\n")
    monkeypatch.setattr(dev, "REPO_ROOT", tmp_path)
    return tmp_path


def restarts_on(watch: dev.ReloadWatch) -> FileFilter:
    """The reloader's own filter, built from what we would pass it."""
    return FileFilter(
        Config(
            dev.APPLICATION,
            factory=True,
            reload=True,
            reload_dirs=[str(directory) for directory in watch.directories],
            reload_includes=list(watch.includes),
            reload_excludes=list(watch.excludes),
        )
    )


# --- what a restart is for --------------------------------------------------


@pytest.mark.parametrize(
    "changed",
    [
        "packages/runtime/src/engine/runtime/session.py",
        "apps/web/src/engine/apps/web/api.py",
        # Read once at startup, which is the whole of "restart if necessary":
        # an edit to either is invisible until the process that read it dies.
        "engine.toml",
        "workflows/implementation_review.py",
    ],
)
def test_a_change_the_running_process_cannot_see_restarts_it(
    checkout: Path, changed: str
) -> None:
    assert restarts_on(dev.reload_watch())(checkout / changed)


@pytest.mark.parametrize(
    "changed",
    [
        # The server writes these itself, on every message. Restarting on them
        # would end the turn that wrote them.
        "conversations.sqlite3",
        "conversations.sqlite3-wal",
        "conversations.sqlite3-shm",
        # Somebody else's files, and the first two are full of `*.py`.
        ".venv/lib/python3.11/site-packages/starlette/applications.py",
        "apps/web/node_modules/some-package/setup.py",
        "apps/web/dist/assets/index.js",
    ],
)
def test_a_change_that_is_not_somebody_editing_source_does_not(
    checkout: Path, changed: str
) -> None:
    assert not restarts_on(dev.reload_watch())(checkout / changed)


def test_agent_worktrees_do_not_restart_the_server_underneath_the_agent(
    checkout: Path,
) -> None:
    """A `workspace_root` inside the checkout is the one that would.

    It defaults to `/tmp`, so this is about the deployment that moves it: an
    agent editing files in its own worktree would otherwise restart the server
    running the turn that made it.
    """
    inside = checkout / "worktrees"
    (inside / "ws-1" / "packages" / "domain").mkdir(parents=True)
    watch = dev.reload_watch(workspace_root=inside)

    assert not restarts_on(watch)(inside / "ws-1" / "packages" / "domain" / "ids.py")
    # And the same file in the checkout proper still does.
    assert restarts_on(watch)(checkout / "packages" / "runtime" / "ids.py")


def test_a_configuration_file_outside_the_checkout_is_watched_where_it_lives(
    checkout: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    elsewhere = tmp_path_factory.mktemp("configuration") / "engine.toml"
    elsewhere.write_text("default_branch = 'main'\n")
    watch = dev.reload_watch(config_path=elsewhere)

    assert elsewhere.parent in watch.directories
    assert restarts_on(watch)(elsewhere)


def test_exclusions_are_absolute(checkout: Path) -> None:
    """The reloader matches them against absolute paths.

    A relative exclusion is not rejected, it is simply never equal to any
    parent of a changed file -- which makes it no exclusion at all, silently.
    """
    directories = [
        exclusion for exclusion in dev.reload_watch().excludes if not exclusion.startswith("*")
    ]
    assert directories
    assert all(Path(exclusion).is_absolute() for exclusion in directories)


# --- what each child is told ------------------------------------------------


def test_the_reload_child_constructs_the_application_it_cannot_be_handed() -> None:
    """`--factory` and an import string, because each child builds its own."""
    command = dev.api_command(host="localhost", port=8000, watch=dev.reload_watch())

    assert "--reload" in command
    assert "--factory" in command
    assert dev.APPLICATION in command

    module_name, _, attribute = dev.APPLICATION.partition(":")
    module = importlib.import_module(module_name)
    assert getattr(module, attribute) is module.build_app


def test_the_children_are_told_what_they_cannot_work_out_for_themselves(
    tmp_path: Path,
) -> None:
    """The config the reload child never saw a command line for, and the port
    the browser tier has to proxy to."""
    config = tmp_path / "engine.toml"
    environment = dev.child_environment(
        {"PATH": "/usr/bin"}, config_path=config, api_url="http://localhost:8123"
    )

    assert environment[CONFIG_ENVIRONMENT_VARIABLE] == str(config)
    assert environment[dev.API_URL_ENVIRONMENT_VARIABLE] == "http://localhost:8123"
    assert environment["PATH"] == "/usr/bin"


def test_defaults_are_left_alone_when_there_is_nothing_to_say() -> None:
    assert dev.child_environment({"PATH": "/usr/bin"}) == {"PATH": "/usr/bin"}


def test_the_browser_tier_is_started_from_the_directory_that_owns_its_scripts() -> None:
    assert dev.web_command() == [
        "npm",
        "--prefix",
        str(dev.WEB_ROOT),
        "run",
        "dev",
        "--",
        "--strictPort",
    ]
    assert dev.web_command(port=4000)[-2:] == ["--port", "4000"]


# --- stopping ---------------------------------------------------------------


def test_sigterm_cleans_up_children_before_the_supervisor_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """launchd uses SIGTERM, so it must take the same cleanup path as Ctrl-C."""
    handlers: dict[signal.Signals, object] = {}
    stopped: list[dev.Child] = []

    def install(sent: signal.Signals, handler: object) -> object:
        previous = handlers.get(sent, signal.SIG_DFL)
        handlers[sent] = handler
        return previous

    process = SimpleNamespace(stdout=[], poll=lambda: None)
    child = dev.Child(name="web", process=process)
    monkeypatch.setattr(signal, "signal", install)
    monkeypatch.setattr(dev, "stop", stopped.append)

    def interrupt(_seconds: float) -> None:
        handler = handlers[signal.SIGTERM]
        assert callable(handler)
        handler(signal.SIGTERM, None)

    monkeypatch.setattr(dev.time, "sleep", interrupt)

    assert dev.supervise([child], lambda _line: None) == 0
    assert stopped == [child]


def test_a_child_that_ignores_the_interrupt_is_killed_anyway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vite and Uvicorn both stop politely; the guarantee cannot rest on that.

    A tier left running holds its port, and the next `engine-dev` then starts
    against half a system somebody has to find with `ps`.
    """
    monkeypatch.setattr(dev, "SHUTDOWN_TIMEOUT_SECONDS", 1.0)
    deaf = dev.start(
        "deaf",
        [
            sys.executable,
            "-c",
            "import signal, time;"
            " signal.signal(signal.SIGINT, signal.SIG_IGN);"
            " print('deaf', flush=True);"
            " time.sleep(60)",
        ],
        os.environ,
    )
    assert deaf.process.stdout is not None
    assert deaf.process.stdout.readline() == "deaf\n", "the child never got to ignore anything"

    dev.stop(deaf)

    assert deaf.process.poll() is not None
    assert deaf.process.returncode == -signal.SIGKILL


# --- changes a restart cannot fix -------------------------------------------


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        ("pyproject.toml", "uv sync --all-packages"),
        ("packages/domain/pyproject.toml", "uv sync --all-packages"),
        ("uv.lock", "uv sync --all-packages"),
        ("apps/web/package.json", "npm --prefix apps/web install"),
        ("apps/web/package-lock.json", "npm --prefix apps/web install"),
    ],
)
def test_a_dependency_change_names_the_command_that_fixes_it(
    changed: str, expected: str
) -> None:
    notice = dev.dependency_notice(dev.REPO_ROOT / changed)

    assert notice is not None
    assert expected in notice
    assert changed in notice


def test_a_source_change_needs_no_advice() -> None:
    assert dev.dependency_notice(dev.REPO_ROOT / "packages/domain/src/engine/domain/ids.py") is None


def test_every_manifest_in_the_workspace_is_watched_for_that() -> None:
    """Including the ones a restart looks like it should have picked up."""
    watched = set(dev.dependency_files())

    assert dev.REPO_ROOT / "uv.lock" in watched
    assert dev.REPO_ROOT / "pyproject.toml" in watched
    assert dev.REPO_ROOT / "apps/web/package.json" in watched
    assert dev.REPO_ROOT / "packages/domain/pyproject.toml" in watched
    assert dev.REPO_ROOT / "packages/adapters/agent_runner/codex/pyproject.toml" in watched
