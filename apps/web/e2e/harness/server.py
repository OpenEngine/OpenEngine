"""Serve the real web application against scripted provider CLIs.

The browser tier's whole point is that nothing between the click and the
subprocess is a stand-in, so this composes the application exactly as
`engine.apps.web.__main__` does -- same capabilities, same runner mapping, same
approval policy plumbing -- and changes only what a test must own:

    where it works        a fixture repository, so worktrees are disposable
    what it remembers     a SQLite file under the test's own directory
    which CLI it runs     `tests/provider_fakes.py`, scripted per test
    GitHub API calls      stubbed so tests run without a real token or network
    which workflows       a directory, when the spec asks for a variant

Everything else is production wiring, including the parts that are easy to get
wrong: the interactive runners, the write-enabled workflow runners, and the
read-only runners that answer both a workflow's reviews and the agents that
never change anything.

Run by `apps/web/e2e/harness.ts`, one process per test, on a port the test
picked. It is not a fixture generator: it starts a server and serves until it
is killed.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

#: apps/web/e2e/harness/server.py -> the repository root.
REPO_ROOT = Path(__file__).resolve().parents[4]

# The fake CLIs are shared with the pytest tier, where they live.
sys.path.insert(0, str(REPO_ROOT / "tests"))

import uvicorn  # noqa: E402

from engine.apps.web import graphs  # noqa: E402
from engine.apps.web.__main__ import STATIC_DIRECTORY  # noqa: E402
from engine.apps.web.api import create_app  # noqa: E402
from engine.apps.web.composition import (  # noqa: E402
    Settings,
    build_capabilities,
    build_read_only_runners,
    build_runners,
    build_session,
    build_workflow_runners,
)
from engine.runtime import (  # noqa: E402
    EngineConfigError,
    LoadedEngineConfig,
    WorkflowCatalog,
    WorkflowLoadError,
    describe_loaded_config,
    load_engine_config,
    load_workflow_catalog,
)
from engine.adapters.source_control.github import GitHubSourceControl  # noqa: E402
import acp_provider_fakes  # noqa: E402
from provider_fakes import fake_claude, fake_codex  # noqa: E402


def _graph_workflows(directory: str, state: Path, binaries: Path) -> WorkflowCatalog:
    """Load the variant this test was pointed at, with a scripted agent behind it.

    Same substitution the rest of this harness makes, one protocol over. The
    variant is an ordinary workflow directory -- `apps/web/e2e/workflows` -- and
    what it needs from the test is named here rather than hardcoded there: the
    ACP agent to launch, and a worktree root that goes away with the test.
    """
    os.environ["ENGINE_E2E_ACP_AGENT"] = acp_provider_fakes.install(
        "acp-agent", binaries
    )
    os.environ["ENGINE_E2E_WORKSPACE_ROOT"] = str(state / "workspaces")
    # Inherited by the agent process the provider spawns, which is where the
    # sessions it has to be able to reload are kept.
    os.environ[acp_provider_fakes.STATE_ENVIRONMENT_VARIABLE] = str(state / "acp")
    return load_workflow_catalog(directory)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument(
        "--repository",
        required=True,
        help="the git repository conversations and workflow runs work in",
    )
    parser.add_argument(
        "--state",
        required=True,
        help="scratch directory for the database, worktrees, and fake CLIs",
    )
    parser.add_argument(
        "--graph-workflows",
        help=(
            "a workflow directory whose definitions run as graphs. Omitted, "
            "this server is the application without a graph runtime in it."
        ),
    )
    parser.add_argument(
        "--config",
        help=(
            "an engine.toml for this run. Omitted, the built-in defaults apply "
            "rather than whatever engine.toml the process was started next to."
        ),
    )
    args = parser.parse_args(argv)

    state = Path(args.state)
    binaries = state / "bin"
    binaries.mkdir(parents=True, exist_ok=True)
    try:
        loaded = load_engine_config(args.config) if args.config else LoadedEngineConfig()
        catalog = (
            _graph_workflows(args.graph_workflows, state, binaries)
            if args.graph_workflows
            else None
        )
    except (EngineConfigError, WorkflowLoadError) as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 2

    if not (STATIC_DIRECTORY / "index.html").is_file():
        print(
            "the assistant-ui client has not been built; "
            "run `npm --prefix apps/web run build`",
            file=sys.stderr,
        )
        return 2

    settings = Settings(
        host="127.0.0.1",
        port=args.port,
        codex_binary=fake_codex(binaries),
        claude_binary=fake_claude(binaries),
        codex_working_directory=args.repository,
        claude_working_directory=args.repository,
        workspace_root=str(state / "workspaces"),
        sqlite_path=str(state / "conversations.sqlite3"),
        engine_config=loaded.config,
        config_path=loaded.path,
    )
    capabilities = build_capabilities(settings)
    runners = build_runners(settings)
    read_only_runners = build_read_only_runners(settings)
    app = create_app(
        build_session(
            capabilities, runners, args.repository, read_only_runners=read_only_runners
        ),
        runners,
        STATIC_DIRECTORY,
        workflow_runners=build_workflow_runners(settings),
        review_runners=read_only_runners,
        approval_policy=loaded.config.approvals,
        default_branch=loaded.config.default_branch,
        graph_runtime=catalog is not None and bool(catalog.graphs),
    )
    if catalog is not None and catalog.graphs:
        app = graphs.serve(app, catalog.graphs, state / "graph-runtime")
    # Stub out real GitHub API calls so e2e tests work without a token.
    # Comment POSTs are recorded to gh.jsonl so tests can assert on them.
    gh_log = state / "gh.jsonl"

    async def _fake_api(self, method: str, path: str, **kwargs: object) -> dict:
        if method == "GET" and "/pulls/" in path:
            return {"head": {"sha": "abc1234"}}
        if method == "POST" and "/comments" in path:
            import json as _json
            body = (kwargs.get("json") or {}).get("body", "")
            with gh_log.open("a", encoding="utf-8") as f:
                f.write(_json.dumps({"path": path, "body": body}) + "\n")
        return {}

    GitHubSourceControl._api = _fake_api  # type: ignore[method-assign]

    print(describe_loaded_config(loaded), flush=True)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
