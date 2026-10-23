"""`oe backend|graph|graphs|run|runs|loop|loops|node|nodes|agent|agents|runner`.

Backend commands speak to one backend -- `--backend`, else `ENGINE_BACKEND`, else
the one `oe backend use` chose -- and print readable lines, or a table for
list commands, unless `--json` asks for JSON. Errors go to stderr; the exit status is 0 on success,
1 on a refusal or a failed run, and 2 on a usage mistake.
`graph spec` and `loop spec` print local Markdown specifications offline.
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from inspect import getdoc
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import yaml

from engine.cli import backends, live, repository, starters
from engine.cli.backends import Backend, BackendError
from engine.cli.http import Client, RequestFailed
from engine.cli.specs import graph_spec, loop_spec

COMMANDS = frozenset({
    "backend", "backends", "graph", "graphs", "run", "runs", "loop", "loops", "node", "nodes", "agent", "agents",
    "runner", "repo", "go", "verb", "verbs",
})
#: Commands omitted from the bare `oe` menu.
HIDDEN = frozenset({"runner", "verb", "verbs", "backends", "graphs", "runs", "loops", "nodes", "agents", "connections"})
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
WAIT_INTERVAL_SECONDS = 2.0
TERMINAL_STEERING = frozenset({"applied", "rejected", "undelivered"})

#: The harnesses an agent runs on.
AGENT_KINDS = ("claude", "codex", "opencode")

#: How each kind of agent signs in on the machine its daemon runs on.
SIGNIN = {
    "codex": (["codex", "login"], ""),
    "claude": (["claude"], "type /login, finish signing in, then /exit"),
    "opencode": (["opencode", "auth", "login"], ""),
}


def add_list_alias(commands: argparse._SubParsersAction, name: str, listing: argparse.ArgumentParser) -> None:
    """Share a singular list parser with its optional-`list` plural alias."""
    alias = commands.add_parser(f"{name}s", parents=[listing], add_help=False, help=f"alias for {name} list")
    alias.add_argument("action", nargs="?", choices=("list",), default="list")
    alias.set_defaults(command=name)


def add_parsers(commands: argparse._SubParsersAction) -> argparse._SubParsersAction:
    """Add these commands to the `oe` parser.

    Answers `oe agent`'s actions, so the terminal app can add the ones that
    run here rather than on a backend, such as `oe agent claude`, `codex` and `opencode`.
    """
    # Readable output is the default; `--pretty` is still accepted so older scripts keep working.
    readable = argparse.ArgumentParser(add_help=False)
    readable.add_argument("--json", action="store_true", help="JSON instead of readable output")
    readable.add_argument("--pretty", action="store_true", help=argparse.SUPPRESS)
    common = argparse.ArgumentParser(add_help=False, parents=[readable])
    common.add_argument("--backend", metavar="NAME", help="the backend to use (default: the selected one)")
    scoped = argparse.ArgumentParser(add_help=False, parents=[common])
    listed = argparse.ArgumentParser(add_help=False)
    listed.add_argument("--json", action="store_true", help="JSON instead of a table")
    listed.add_argument("--pretty", action="store_true", help=argparse.SUPPRESS)
    scoped.add_argument("--project", metavar="NAME", help="the project names resolve in (default: the backend's)")

    go = commands.add_parser(
        "go", parents=[scoped], help="make a change with the default graph and follow it live",
        description="Follows live on a TTY. With --json, prints JSON and sends the mode banner to stderr.",
        epilog='examples:\n  oe go "add a widget"\n  oe go fix https://github.com/OpenEngine/OpenEngine/pull/855',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    go.add_argument("words", nargs="*", metavar="VERB/TARGET/INSTRUCTION", help="instruction, or a verb and change target")
    go.add_argument("--trust", action=argparse.BooleanOptionalAction, default=None, help="remember whether agents may run without approval here")
    go.add_argument("--repo", default="", help="repository (default: the current checkout)")
    go.add_argument("--local", action="store_true", help="force disconnected mode; keep work local")
    go.add_argument("--review", metavar="PR_OR_BRANCH", help="review an existing change with the review verb graph")
    go.add_argument("--spec", action="store_true", help="write a specification before implementing")
    go.add_argument("--input", "-i", action="append", default=[], metavar="KEY=VALUE")
    go.add_argument("--wait", action="store_true", help="wait non-interactively for completion")
    go.add_argument("--timeout", type=float, default=0.0, metavar="SECONDS")
    go.set_defaults(graph=starters.DEFAULT, branch="", agent="", no_watch=False, idempotency_key="")

    verb = commands.add_parser("verb", help="bind a project verb to a graph")
    actions = verb.add_subparsers(dest="action", required=True)
    listing = actions.add_parser("list", parents=[scoped], help="show the project's verb bindings")
    add_list_alias(commands, "verb", listing)
    setting = actions.add_parser("set", parents=[scoped], help="bind a verb after checking the graph's inputs")
    setting.add_argument("verb")
    setting.add_argument("graph")

    backend = commands.add_parser("backend", help="choose which engine daemon commands talk to")
    actions = backend.add_subparsers(dest="action", required=True)
    adding = actions.add_parser("add", parents=[readable], help="name a daemon URL, such as the Mac mini's")
    adding.add_argument("name")
    adding.add_argument("url", help="such as http://mac-mini.local:4364")
    adding.add_argument("--project", default=backends.DEFAULT_PROJECT, help="project graph names resolve in")
    adding.add_argument("--token-env", default="", metavar="VAR", help="environment variable holding its bearer token")
    adding.add_argument("--repo", default="", help="repository runs check out when none is given")
    adding.add_argument("--use", action="store_true", help="also make it the selected backend")
    adding.add_argument("--replace", action="store_true", help="overwrite a backend with this name")
    for name, help_text in (("list", "list backends"), ("use", "select a backend"), ("remove", "forget a backend")):
        action = actions.add_parser(name, parents=[listed] if name == "list" else [readable], help=help_text)
        if name != "list":
            action.add_argument("name")
        else:
            action.add_argument("--check", action="store_true", help="also ask each backend whether it is up")
    add_list_alias(commands, "backend", actions.choices["list"])

    repo = commands.add_parser("repo", help="add repositories and manage trust")
    repo_actions = repo.add_subparsers(dest="action", required=True)
    onboard = repo_actions.add_parser("add", parents=[common], help="add a local checkout or clone a GitHub/GitLab URL")
    onboard.add_argument("--review-backend", metavar="NAME", help="backend with service credentials for the suggested review work order")
    onboard.add_argument("--trust", "--trusted", action=argparse.BooleanOptionalAction, default=None, help="remember whether agents may run without approval here")
    onboard.add_argument("url", nargs="?", default=".", metavar="repository", help="existing checkout path or GitHub/GitLab URL (default: .)")
    onboard.add_argument("--path", help="clone destination on the daemon machine (default: [onboarding] directory, else ~/code/repo)")
    onboard.add_argument("--mode", choices=("connected", "disconnected"), default=None, help="repository workflow mode (default: connected with a forge origin, otherwise disconnected)")

    for name in ("trust", "untrust"):
        setting = repo_actions.add_parser(name, parents=[common],
            help="auto-approve runs in a repository" if name == "trust" else "require approvals in a repository")
        setting.add_argument("repo", nargs="?", default="", metavar="path", help="checkout path or configured name (default: current checkout)")

    graph = commands.add_parser("graph", help="register, inspect and run graphs")
    actions = graph.add_subparsers(dest="action", required=True)
    actions.add_parser(
        "spec", help="print the current graph specification",
        description=getdoc(graph_spec), formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    adding = actions.add_parser("add", parents=[scoped], help="register a graph (YAML or a Python file)")
    adding.add_argument("file", help="the graph file, or - for YAML on stdin")
    getting = actions.add_parser("get", parents=[scoped], help="a graph and its exact definition")
    getting.add_argument("graph", help="a name, name@VERSION, graph id or version id")
    running = actions.add_parser(
        "run", parents=[scoped], help="run a graph with an instruction",
        description="Follows live when stdin and stdout are TTYs, unless --wait, --json or --no-watch is given.",
    )
    running.add_argument("graph", help=f"a graph, or a built-in one: {', '.join(starters.NAMES)}")
    running.add_argument("instruction", nargs="?", default="", help="what to do (default: run the graph)")
    running.add_argument("--input", "-i", action="append", default=[], metavar="NAME=VALUE")
    running.add_argument("--branch", default="", help="the branch to check out (sets the branch input)")
    running.add_argument("--agent", default="", help="the agent to run (sets the agent input)")
    running.add_argument("--local", action="store_true", help="keep commits and findings local; do not publish")
    running.add_argument("--trust", action=argparse.BooleanOptionalAction, default=None, help="remember whether agents may run without approval here")
    running.add_argument("--repo", default="", help="repository to check out")
    running.add_argument("--no-watch", action="store_true", help="return immediately without the live view")
    running.add_argument("--wait", action="store_true", help="wait for the run to finish")
    running.add_argument("--timeout", type=float, default=0.0, metavar="SECONDS", help="give up waiting after this long")
    running.add_argument("--idempotency-key", default="", help="reuse to make a resubmission safe")
    graphs = actions.add_parser("list", parents=[listed], help="list graphs")
    graphs.add_argument("--backend", metavar="NAME")
    graphs.add_argument("--project", metavar="NAME")
    graphs.add_argument("--all-projects", action="store_true")
    add_list_alias(commands, "graph", graphs)

    run = commands.add_parser("run", help="inspect a run")
    actions = run.add_subparsers(dest="action", required=True)
    getting = actions.add_parser("get", parents=[common], help="status, nodes, results and failure")
    getting.add_argument("run_id")
    watching = actions.add_parser("watch", parents=[common], help="follow a run live and answer approvals inline")
    watching.add_argument("run_id")
    waiting = actions.add_parser("wait", parents=[common], help="wait for a run to finish")
    waiting.add_argument("run_id")
    waiting.add_argument("--timeout", type=float, default=0.0, metavar="SECONDS")
    runs = actions.add_parser("list", parents=[listed], help="list runs, newest first")
    runs.add_argument("--backend", metavar="NAME")
    runs.add_argument("--project", metavar="NAME")
    runs.add_argument("--all-projects", action="store_true")
    runs.add_argument("--graph", default="", help="only runs of this graph")
    runs.add_argument("--loop", default="", help="only runs this loop started")
    runs.add_argument("--requester", default="", help="only runs requested by this identity")
    runs.add_argument("--origin", default="", help="only runs from this origin channel")
    runs.add_argument("--status", default="", help="only runs in this status, such as running or failed")
    runs.add_argument("--limit", type=int, default=20, help="at most this many (default: 20)")
    add_list_alias(commands, "run", runs)

    loop = commands.add_parser("loop", help="recurring runs of a graph, within limits")
    actions = loop.add_subparsers(dest="action", required=True)
    actions.add_parser(
        "spec", help="print the current loop specification",
        description=getdoc(loop_spec), formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    adding = actions.add_parser("add", parents=[scoped], help="run a graph on a cadence")
    adding.add_argument("graph")
    adding.add_argument("--name", default="", help="loop name (default: the graph's)")
    adding.add_argument("--instruction", default="", help="override the graph's loop.instruction")
    adding.add_argument("--every", default=None, help="cadence such as 30m, 6h, 1d (default: the graph's loop.every)")
    adding.add_argument("--max-prs", type=int, default=None, help="pause after this many new pull requests")
    adding.add_argument("--max-spend", type=float, default=None, metavar="USD", help="pause, and stop the active run, at this spend")
    adding.add_argument("--trust", action=argparse.BooleanOptionalAction, default=None, help="remember whether agents may run without approval here")
    adding.add_argument("--repo", default="")
    adding.add_argument("--input", "-i", action="append", default=[], metavar="NAME=VALUE")
    adding.add_argument("--no-run-now", action="store_true", help="first run after one interval, not now")
    for name, help_text in (("get", "one loop's state and budget"), ("pause", "stop starting runs"), ("resume", "start runs again"), ("stop", "permanently stop the loop and its active run")):
        action = actions.add_parser(name, parents=[scoped], help=help_text)
        action.add_argument("loop", help="a loop name or id")
        if name == "pause":
            action.add_argument("--reason", default="")
        if name == "resume":
            action.add_argument("--max-prs", type=int, default=None)
            action.add_argument("--max-spend", type=float, default=None, metavar="USD")
    loops = actions.add_parser("list", parents=[listed], help="list loops")
    loops.add_argument("--backend", metavar="NAME")
    loops.add_argument("--project", metavar="NAME")
    loops.add_argument("--all-projects", action="store_true")
    add_list_alias(commands, "loop", loops)

    node = commands.add_parser("node", help="inspect or steer one node execution")
    actions = node.add_subparsers(dest="action", required=True)
    nodes = actions.add_parser("list", parents=[listed], help="list a run's node executions")
    nodes.add_argument("--run", required=True, metavar="RUN_ID")
    nodes.add_argument("--backend", metavar="NAME")
    add_list_alias(commands, "node", nodes)
    getting = actions.add_parser("get", parents=[common], help="one node execution and its steering")
    getting.add_argument("execution_id")
    steering = actions.add_parser("steer", parents=[common], help="send a running node an updated instruction")
    steering.add_argument("execution_id")
    steering.add_argument("message")
    steering.add_argument("--idempotency-key", default="", help="reuse to make a resend safe")
    steering.add_argument("--wait", action="store_true", help="wait until it is applied or cannot be")
    steering.add_argument("--timeout", type=float, default=0.0, metavar="SECONDS")

    agent = commands.add_parser("agent", help="list, add, remove and sign in agents")
    actions = agent_actions = agent.add_subparsers(dest="action", required=True)
    adding = actions.add_parser("add", parents=[common], help="offer claude, codex or opencode under a name")
    adding.add_argument("kind", choices=AGENT_KINDS)
    adding.add_argument("--name", default="", help="what graphs call it (default: the kind)")
    adding.add_argument("--model", default="", help="the model it runs when a node names none")
    adding.add_argument("--url", default="", help="an OpenAI-compatible endpoint (opencode only; needs --model)")
    adding.add_argument(
        "--context", type=int, metavar="TOKENS",
        help="the model's context window, so opencode compacts before it (needs --url; match Ollama's num_ctx)",
    )
    adding.add_argument("--output", type=int, metavar="TOKENS", help="the most tokens one reply may use (needs --context)")
    adding.add_argument("--replace", action="store_true", help="overwrite an agent with this name")
    for name, help_text in (("get", "one agent"), ("remove", "stop offering an added agent")):
        action = actions.add_parser(name, parents=[common], help=help_text)
        action.add_argument("name")
    signin = actions.add_parser("signin", help="sign an agent in where the backend runs")
    signin.add_argument("name", help="an agent name, such as claude")
    signin.add_argument("--backend", metavar="NAME")
    agents = actions.add_parser("list", parents=[listed], help="list agents")
    agents.add_argument("--backend", metavar="NAME")
    add_list_alias(commands, "agent", agents)

    # `oe runner signin` before agents had their own command; kept for one release.
    runner = commands.add_parser("runner", help="legacy agent sign-in command")
    actions = runner.add_subparsers(dest="action", required=True)
    signin = actions.add_parser("signin")
    signin.add_argument("name", choices=sorted(SIGNIN))
    signin.add_argument("--backend", metavar="NAME")
    return agent_actions


def main(arguments: argparse.Namespace) -> int:
    try:
        handler = _HANDLERS[(arguments.command, getattr(arguments, "action", None))]
        arguments.pretty = not getattr(arguments, "json", False)
        return handler(arguments)
    except (BackendError, RequestFailed, _UsageError, OSError) as error:
        if isinstance(error, _UsageError):
            print(f"oe: {error}", file=sys.stderr)
            return EXIT_USAGE
        _report(error)
        return EXIT_FAILED


def onboard_repository(arguments: argparse.Namespace) -> int:
    backend, _ = _connect(arguments)
    review_backend = backends.load().get(arguments.review_backend) if arguments.review_backend else backend
    if arguments.review_backend and review_backend.token_env == "ENGINE_ONBOARDING_TOKEN":
        raise _UsageError("--review-backend must use service credentials, not ENGINE_ONBOARDING_TOKEN")
    client = Client(backend, api_prefix="/api")
    reference, path = arguments.url, arguments.path
    if backend.is_local:
        local = Path(reference).expanduser()
        if local.exists() or reference.startswith((".", "/", "~")):
            root = repository._git(str(local.resolve()), "rev-parse", "--show-toplevel")
            if not root:
                raise _UsageError("Choose an existing git checkout, or pass a GitHub/GitLab URL")
            reference = root
            if path and Path(path).expanduser().resolve() != Path(root).resolve():
                raise _UsageError("--path is a clone destination; omit it when adding an existing checkout")
            path = root
        elif path:
            path = str(Path(path).expanduser().resolve())
    mode = arguments.mode
    state = client.post("/repositories/onboard", {"repository": reference, "path": path, "mode": mode, "trusted": arguments.trust})
    previous = None
    while True:
        if state["message"] != previous:
            print(state["message"])
            previous = state["message"]
        if state["status"] == "failed":
            for problem in state["problems"]:
                print(f"{problem['message']}. Fix: {problem['fix']}", file=sys.stderr)
            return EXIT_FAILED
        if state["status"] == "complete":
            if review_backend.token_env == "ENGINE_ONBOARDING_TOKEN":
                print("For a review command, repeat repo add with --review-backend NAME, "
                      "using a backend for this daemon configured with its service token.")
                return EXIT_OK
            command = ["oe", "go", "review", "--project", state["repository"],
                       "--repo", state["path"], "--backend", review_backend.name, "--input", "ref=HEAD"]
            print(f"Next: {shlex.join(command)}")
            return EXIT_OK
        time.sleep(WAIT_INTERVAL_SECONDS)
        state = client.get(f"/repositories/onboard/{state['id']}")


# --- backends ---------------------------------------------------------------


def backend_add(arguments: argparse.Namespace) -> int:
    config = backends.add(
        arguments.name, arguments.url, project=arguments.project, token_env=arguments.token_env,
        repository=arguments.repo, use=arguments.use, replace_existing=arguments.replace,
    )
    added = config.get(arguments.name)
    _emit(added.json(current=config.current == added.name), arguments.pretty, _backend_line)
    return EXIT_OK


def backend_list(arguments: argparse.Namespace) -> int:
    config = backends.load()
    rows = []
    for backend in config.all().values():
        row = backend.json(current=backend.name == config.current)
        if getattr(arguments, "check", False):
            row["health"] = _health(backend)
        rows.append(row)
    if not arguments.json:
        _table(rows, [("", lambda r: "*" if r["current"] else " "), ("NAME", "name"), ("URL", "url"),
                      ("PROJECT", "project"), ("TOKEN", lambda r: r["token_env"] or "-"),
                      *([("HEALTH", "health")] if getattr(arguments, "check", False) else [])])
    else:
        _print({"current": config.current, "backends": rows})
    return EXIT_OK


def backend_use(arguments: argparse.Namespace) -> int:
    config = backends.use(arguments.name)
    _emit(config.get(arguments.name).json(current=True), arguments.pretty, _backend_line)
    return EXIT_OK


def backend_remove(arguments: argparse.Namespace) -> int:
    config = backends.remove(arguments.name)
    _emit({"removed": arguments.name, "current": config.current}, arguments.pretty,
          lambda body: f"removed {body['removed']}; selected backend is {body['current']}")
    return EXIT_OK


# --- graphs -----------------------------------------------------------------


def graph_add(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    format, source = _read_graph(arguments.file)
    try:
        graph = client.post("/graphs", {"project": _project(arguments, backend), "format": format, "source": source})
    except RequestFailed as refused:
        if refused.payload.get("problems"):
            for problem in refused.payload["problems"]:
                print(f"  {problem['path']}: {problem['message']}", file=sys.stderr)
        raise
    _emit(graph, arguments.pretty, lambda g: (
        f"{'registered' if g['created'] else 'unchanged'} {g['name']} v{g['version']} "
        f"({g['graphId']}, version {g['versionId']}) in project {g['project']}"
    ))
    return EXIT_OK


def graphs_list(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    project = None if arguments.all_projects else _project(arguments, backend)
    graphs = client.get("/graphs", project=project)["graphs"]
    if not arguments.json:
        _table(graphs, [("NAME", "name"), ("VERSION", "version"), ("PROJECT", "project"),
                        ("GRAPH ID", "graphId"), ("UPDATED", "updatedAt"), ("DESCRIPTION", "description")])
    else:
        _print({"graphs": graphs})
    return EXIT_OK


def graph_get(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    graph = client.get(f"/graphs/{quote(arguments.graph, safe='@')}", project=_project(arguments, backend))
    if arguments.pretty:
        print(f"{graph['name']} v{graph['version']}  ({graph['graphId']}, version {graph['versionId']})")
        print(f"project: {graph['project']}")
        if graph.get("description"):
            print(graph["description"])
        print("versions: " + ", ".join(f"v{v['version']} {v['versionId']}" for v in graph.get("versions", [])))
        print()
        # What was registered, as written; `definition` is the backend's parse of it.
        print((graph.get("source") or yaml.safe_dump(graph["definition"], sort_keys=False)).rstrip())
    else:
        _print(graph)
    return EXIT_OK


def _ensure_graph(client: Client, project: str, graph: str) -> dict[str, Any]:
    try:
        found = client.get(f"/graphs/{quote(graph, safe='')}", project=project)
    except RequestFailed as error:
        if graph not in starters.NAMES or error.status != 404:
            raise
        found = {}
    if graph in starters.NAMES and (not found or found.get("format") == "yaml"):
        found = client.post("/graphs", {"project": project, "format": "python", "source": starters.source(graph)})
    return found


def _contract(graph: dict[str, Any], verb: str) -> None:
    declared = set(graph.get("inputs", []))
    if not ("instruction" in declared if verb == "go" else declared.intersection({"pr_url", "ref"})):
        required = "instruction" if verb == "go" else "pr_url or ref"
        raise _UsageError(f"graph {graph.get('name')!r} cannot bind {verb!r}: it must declare {required}")


def verbs_list(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    _print(client.get("/verbs", project=_project(arguments, backend)))
    return EXIT_OK


def verb_set(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    project = _project(arguments, backend)
    _contract(_ensure_graph(client, project, arguments.graph), arguments.verb)
    _print(client.post(f"/verbs/{quote(arguments.verb, safe='')}", {"project": project, "graph": arguments.graph}))
    return EXIT_OK


def go(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    project = _project(arguments, backend)
    verbs = client.get("/verbs", project=project)["verbs"]
    words, verb, target = arguments.words, "go", {}
    if arguments.review:
        words = ["review", arguments.review, *words]
    repo = _repository(arguments, backend)
    if words and words[0] in verbs and words[0] != "go":
        candidate = words[1] if len(words) > 1 else ""
        inferred = not candidate
        if inferred and not arguments.repo and (checkout := repository.current()):
            candidate = checkout.branch
        try:
            target = client.post("/targets/resolve", {"repository": repo, "target": candidate,
                                "local": arguments.local, "verb": words[0]})
        except RequestFailed as error:
            # Multiword prose beginning with a verb stays an implementation instruction.
            # A sole missing target is a useful typo error; explicit PR syntax is never prose.
            explicit = candidate.startswith(("https://", "#")) or "#" in candidate or "/" in candidate
            if error.status != 404 or len(words) == 2 or explicit:
                raise
        else:
            verb, words = words[0], words[2:] if not inferred else []
    if verb == "go" and not words:
        raise _UsageError("give an instruction or a verb with an inferable target")
    if not target:
        target = client.post("/targets/resolve", {"repository": repo, "target": "", "local": arguments.local, "verb": "go"})
    arguments.graph = verbs[verb]
    graph = _ensure_graph(client, project, arguments.graph)
    _contract(graph, verb)
    declared = set(graph.get("inputs", []))
    if arguments.spec:
        arguments.input.append("spec=true")
    supplied = _inputs(arguments.input)
    for name in ("pr_url", "ref", "review_threads"):
        if name in target and name in declared and name not in supplied:
            arguments.input.append(f"{name}={target[name]}")
    if verb != "go" and not any(name in declared and target.get(name) for name in ("pr_url", "ref")):
        raise _UsageError("the target does not provide an input accepted by this graph (pr_url or ref)")
    arguments.repo = target["repo"]
    arguments.instruction = " ".join(words) or f"{verb.capitalize()} this change"
    arguments.verb, arguments.target = verb, target
    arguments.pretty = not arguments.json
    return graph_run(arguments)


def _submit_run(client: Client, request: dict[str, Any]) -> dict[str, Any]:
    try:
        return client.post("/runs", request, idempotent=True)
    except RequestFailed as refused:
        unloaded = refused.status == 503 and "is not loaded" in str(refused)
        if request["graph"] not in starters.NAMES or not (refused.status == 404 or unloaded):
            raise
        client.post("/graphs", {"project": request["project"], "format": "python",
                                "source": starters.source(request["graph"])})
        return client.post("/runs", request, idempotent=True)


def graph_run(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    key = arguments.idempotency_key or f"cli-{uuid.uuid4()}"
    inputs = _inputs(arguments.input)
    graph = arguments.graph
    if arguments.command != "go" and graph in starters.ALIASES:
        print(f"oe: {graph} is deprecated; use oe go instead (alias kept for one release)", file=sys.stderr)
        graph, defaults = starters.ALIASES[graph]
        inputs = {**defaults, **inputs}
        _ensure_graph(client, _project(arguments, backend), graph)
    if graph in starters.NAMES:
        if arguments.branch:
            inputs["ref"] = arguments.branch
        if arguments.agent or inputs.get("agent"):
            agent = arguments.agent or inputs["agent"]
            inputs.pop("agent", None)
            inputs.update(implementation_runner=agent, review_runner=agent)
    if getattr(arguments, "local", False):
        inputs["mode"] = "disconnected"
    for name in ("branch", "agent"):
        if getattr(arguments, name) and graph not in starters.NAMES:
            inputs[name] = getattr(arguments, name)
    request = {
        "project": _project(arguments, backend),
        "graph": graph,
        "instruction": arguments.instruction or _default_instruction(arguments),
        "inputs": inputs,
        "repository": _repository(arguments, backend),
        "idempotencyKey": key,
    }
    _check_trust(arguments, client, request["repository"])
    run = _submit_run(client, request)
    if arguments.command == "go" and (context := run.get("forgeContext")):
        target = arguments.target.get("pr_url") or arguments.target.get("ref") or request["repository"]
        if arguments.target.get("pr_url"):
            parts = urlsplit(arguments.target["pr_url"]).path.strip("/").split("/")
            if len(parts) == 4 and parts[2] == "pull":
                target = f"{parts[0]}/{parts[1]}#{parts[3]}"
        effect = ("will post review comments" if arguments.verb == "review" else
                  "will push fixes and reply to review threads" if arguments.verb == "fix" else "will open a pull request")
        if arguments.verb == "review" and not arguments.target.get("pr_url"):
            effect = "will keep findings in run results"
        if context["mode"] == "disconnected":
            effect = "will keep results and commits local"
        print(live.safe_text(f"{graph} · {context['mode']} · {target} · {effect}"),
              file=sys.stderr if arguments.json else sys.stdout, flush=True)
    if not (arguments.wait or arguments.json or arguments.no_watch) and sys.stdout.isatty() and sys.stdin.isatty():
        return _watch_run(client, run["runId"], backend)
    if arguments.json:
        arguments.pretty = False
    if arguments.wait:
        if not arguments.pretty:
            print(json.dumps({"runId": run["runId"], "status": run["status"]}), file=sys.stderr)
        else:
            print(f"started {run['runId']} ({run['graph']} v{run['version']}); waiting...", file=sys.stderr)
        run = _wait_for_run(client, run["runId"], arguments.timeout)
    if arguments.command == "go" and not arguments.json:
        for line in live.summary(run, backend.name):
            print(line)
        return EXIT_FAILED if _run_failed(run) else EXIT_OK
    return _show_run(run, backend, arguments.pretty)


def runs_list(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    project = None if arguments.all_projects else _project(arguments, backend)
    runs = client.get(
        "/runs", project=project, graph=arguments.graph or None, loop=arguments.loop or None,
        status=arguments.status or None, limit=str(arguments.limit),
        requester=arguments.requester or None, origin=arguments.origin or None,
    )["runs"]
    if not arguments.json:
        _table(runs, [
            ("RUN ID", "runId"), ("NAME", "name"), ("GRAPH", lambda r: f"{r['graph']} v{r['version']}" if r["version"] is not None else r["graph"]), ("STATUS", "status"),
            ("LOOP", "loop"), ("STARTED", "startedAt"), ("SPEND", _run_spend),
        ])
    else:
        _print({"runs": runs})
    return EXIT_OK


def run_get(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    run = client.get(f"/runs/{quote(arguments.run_id)}")
    if arguments.pretty:
        print(live.safe_text(f"UI: {backend.url.rstrip('/')}/runs/{run['runId']}"))
        for approval in run.get("pendingApprovals") or []:
            command = approval.get("command") or approval.get("reason") or ""
            print(live.safe_text(f"approval {approval['approvalId']} at {approval['node']}: {command}"))
    return _show_run(run, backend, arguments.pretty)


def run_watch(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    return _watch_run(client, arguments.run_id, backend)


def _watch_run(client: Client, run_id: str, backend: Backend) -> int:
    run, outcome = live.Watch(client, run_id).follow()
    if outcome == "detached":
        command = shlex.join(["oe", "run", "watch", run_id, "--backend", backend.name])
        print(live.safe_text(f"Detached. Reattach with: {command}"))
        return EXIT_OK
    for line in live.summary(run, backend.name):
        print(line)
    return EXIT_FAILED if outcome == "cancelled" or _run_failed(run) else EXIT_OK


def run_wait(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    return _show_run(_wait_for_run(client, arguments.run_id, arguments.timeout), backend, arguments.pretty)


# --- loops ------------------------------------------------------------------


def loop_add(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    _check_trust(arguments, client, _repository(arguments, backend))
    loop = client.post("/loops", {
        "project": _project(arguments, backend),
        "graph": arguments.graph,
        "name": arguments.name,
        "instruction": arguments.instruction,
        "every": arguments.every,
        "maxPrs": arguments.max_prs,
        "maxSpendUsd": arguments.max_spend,
        "repository": _repository(arguments, backend),
        "inputs": _inputs(arguments.input),
        "startNow": not arguments.no_run_now,
    })
    _emit(loop, arguments.pretty, _loop_summary)
    if loop["limits"]["maxSpendUsd"] is not None:
        print(f"note: max-spend counts {loop['limits']['spendScope'][0].lower()}{loop['limits']['spendScope'][1:]}",
              file=sys.stderr)
    return EXIT_OK


def loops_list(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    project = None if arguments.all_projects else _project(arguments, backend)
    loops = client.get("/loops", project=project)["loops"]
    if not arguments.json:
        _table(loops, [
            ("NAME", "name"), ("GRAPH", lambda l: f"{l['graph']} v{l['version']}"), ("STATE", "state"),
            ("EVERY", lambda l: _duration(l["everySeconds"])),
            ("NEXT", lambda l: l["nextRunAt"] or "-"), ("ACTIVE RUN", lambda l: l["activeRunId"] or "-"),
            ("PRS", lambda l: f"{l['prCount']}/{l['limits']['maxPrs'] or '∞'}"),
            ("SPEND", _spend), ("PAUSED BECAUSE", lambda l: l["pauseReason"] or "-"),
        ])
    else:
        _print({"loops": loops})
    return EXIT_OK


def loop_get(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    _emit(client.get(f"/loops/{quote(arguments.loop)}", project=_project(arguments, backend)),
          arguments.pretty, _loop_summary)
    return EXIT_OK


def loop_pause(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    loop = client.post(f"/loops/{quote(arguments.loop)}/pause",
                       {"project": _project(arguments, backend), "reason": arguments.reason})
    _emit(loop, arguments.pretty, _loop_summary)
    return EXIT_OK


def loop_stop(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    loop = client.post(f"/loops/{quote(arguments.loop)}/stop",
                       {"project": _project(arguments, backend)})
    _emit(loop, arguments.pretty, _loop_summary)
    return EXIT_OK


def loop_resume(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    loop = client.post(f"/loops/{quote(arguments.loop)}/resume", {
        "project": _project(arguments, backend),
        "maxPrs": arguments.max_prs,
        "maxSpendUsd": arguments.max_spend,
    })
    _emit(loop, arguments.pretty, _loop_summary)
    return EXIT_OK


# --- nodes ------------------------------------------------------------------


def nodes_list(arguments: argparse.Namespace) -> int:
    _backend, client = _connect(arguments)
    nodes = client.get(f"/runs/{quote(arguments.run)}/nodes")["nodes"]
    if not arguments.json:
        _table(nodes, _NODE_COLUMNS)
    else:
        _print({"nodes": nodes})
    return EXIT_OK


def node_get(arguments: argparse.Namespace) -> int:
    _backend, client = _connect(arguments)
    node = client.get(f"/nodes/{quote(arguments.execution_id)}")
    if arguments.pretty:
        _table([node], _NODE_COLUMNS)
        if node.get("steering"):
            print()
            _table(node["steering"], _STEERING_COLUMNS)
    else:
        _print(node)
    return EXIT_OK


def node_steer(arguments: argparse.Namespace) -> int:
    _backend, client = _connect(arguments)
    key = arguments.idempotency_key or f"cli-{uuid.uuid4()}"
    path = f"/nodes/{quote(arguments.execution_id)}/steering"
    steering = client.post(path, {"message": arguments.message, "idempotencyKey": key}, idempotent=True)
    if arguments.wait:
        steering = _wait_for_steering(client, arguments.execution_id, steering["steeringId"], arguments.timeout)
    _emit(steering, arguments.pretty, lambda s: (
        f"{s['steeringId']} {s['status']}" + (f": {s['error']}" if s.get("error") else "")
    ))
    return EXIT_OK if steering["status"] not in ("rejected", "undelivered") else EXIT_FAILED


# --- agents -----------------------------------------------------------------


def agent_add(arguments: argparse.Namespace) -> int:
    _backend, client = _connect(arguments)
    agent = client.post("/agents", {
        "kind": arguments.kind, "name": arguments.name, "model": arguments.model,
        "url": arguments.url, "contextLimit": arguments.context, "outputLimit": arguments.output,
        "replace": arguments.replace,
    })
    _emit(agent, arguments.pretty, _agent_line)
    return EXIT_OK


def agents_list(arguments: argparse.Namespace) -> int:
    _backend, client = _connect(arguments)
    agents = client.get("/agents")["agents"]
    if not arguments.json:
        _table(agents, _AGENT_COLUMNS)
        for agent in agents:
            if agent.get("models") is not None:
                print("\n" + _runner_detail(agent))
    else:
        _print({"agents": agents})
    return EXIT_OK


def agent_get(arguments: argparse.Namespace) -> int:
    _backend, client = _connect(arguments)
    _emit(client.get(f"/agents/{quote(arguments.name)}"), arguments.pretty, _agent_line)
    return EXIT_OK


def agent_remove(arguments: argparse.Namespace) -> int:
    _backend, client = _connect(arguments)
    removed = client.delete(f"/agents/{quote(arguments.name)}")
    _emit(removed, arguments.pretty, lambda body: f"removed {body['name']}")
    return EXIT_OK


def agent_signin(arguments: argparse.Namespace) -> int:
    """Sign in the harness an agent runs on, on the machine its backend runs on."""
    backend, client = _connect(arguments)
    if arguments.name in SIGNIN:
        kind, url = arguments.name, ""
    else:
        agent = client.get(f"/agents/{quote(arguments.name)}")
        kind, url = agent["kind"], agent["url"]
    if url:
        print(f"{arguments.name} reaches its model at {url}; it has nothing to sign in to.")
        return EXIT_OK
    command, follow_up = SIGNIN[kind]
    shown = " ".join(command)
    if not backend.is_local:
        host = urlsplit(backend.url).hostname
        print(
            f"{arguments.name} signs in on the machine backend {backend.name} runs on. There, as the "
            f"user its daemon runs as, run:\n\n    {shown}\n"
            + (f"\nthen {follow_up}.\n" if follow_up else "")
            + f"\nFor example: ssh -t {host} {shown}"
        )
        return EXIT_OK
    if shutil.which(command[0]) is None:
        print(f"oe: {command[0]} is not installed here; install it, then run `oe daemon setup`",
              file=sys.stderr)
        return EXIT_FAILED
    if follow_up:
        print(f"Starting {shown}: {follow_up}.", file=sys.stderr)
    return subprocess.call(command)


def runner_signin(arguments: argparse.Namespace) -> int:
    print(f"oe: `oe runner signin` is now `oe agent signin {arguments.name}`", file=sys.stderr)
    return agent_signin(arguments)


# --- helpers ----------------------------------------------------------------


class _UsageError(ValueError):
    pass


def _connect(arguments: argparse.Namespace) -> tuple[Backend, Client]:
    backend = backends.load().selected(getattr(arguments, "backend", None))
    return backend, Client(backend)


def _project(arguments: argparse.Namespace, backend: Backend) -> str:
    return getattr(arguments, "project", None) or backend.project


def _repository(arguments: argparse.Namespace, backend: Backend) -> str:
    """`--repo`, else the repository you are in, else the backend's default.

    A local daemon is sent the checkout's path; a remote one its `owner/repo`.
    """
    if arguments.repo:
        return arguments.repo
    here = repository.current()
    if here is not None:
        return here.target(local=backend.is_local)
    return backend.repository


def _check_trust(arguments: argparse.Namespace, client: Client, repo: str) -> None:
    decision = getattr(arguments, "trust", None)
    backend_info = None
    if not repo:
        backend_info = client.get("/backend")
        if backend_info.get("autoApprove") and decision is None:
            return
        graph = getattr(arguments, "graph", "")
        definition = {}
        if graph:
            try:
                definition = client.get(f"/graphs/{quote(graph, safe='')}",
                    project=getattr(arguments, "project", None) or client.backend.project).get("definition", {})
            except RequestFailed as error:
                if error.status != 404:
                    raise
        repo = definition.get("repository") or backend_info.get("defaultRepository", "")
        if not repo:
            if decision is not None:
                raise _UsageError("Pass --repo or run from a checkout to set repository trust")
            return
    if decision is not None:
        client.post("/repositories/trust", {"repository": repo, "trusted": decision})
        return
    if (backend_info or client.get("/backend")).get("autoApprove"):
        return
    state = client.get("/repositories/trust", repository=repo)
    if state["trusted"] is not None:
        return
    if not getattr(arguments, "json", False) and sys.stdin.isatty() and sys.stdout.isatty():
        answer = input(f"Trust {state['name']} ({state['repository']})? Agents will edit files and run commands here without asking. [y/N] ")
        client.post("/repositories/trust", {"repository": repo, "trusted": answer.strip().lower() in {"y", "yes"}})
    else:
        print("oe: repository is untrusted; use oe repo trust to remember your choice.", file=sys.stderr)


def repo_trust(arguments: argparse.Namespace) -> int:
    backend, client = _connect(arguments)
    repo = _repository(arguments, backend)
    if backend.is_local and arguments.repo and Path(arguments.repo).expanduser().exists():
        repo = str(Path(arguments.repo).expanduser().resolve())
    _print(client.post("/repositories/trust", {"repository": repo, "trusted": arguments.action == "trust"}))
    return EXIT_OK


def _read_graph(file: str) -> tuple[str, str]:
    """The graph's format and its source, sent verbatim so the backend keeps it."""
    text = sys.stdin.read() if file == "-" else Path(file).read_text(encoding="utf-8")
    if file.endswith(".py"):
        return "python", text
    try:
        manifest = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise _UsageError(f"{file} is not valid YAML or JSON: {error}") from None
    if not isinstance(manifest, dict):
        raise _UsageError(f"{file} must hold a mapping")
    return "yaml", text


def _default_instruction(arguments: argparse.Namespace) -> str:
    """What a run is asked when no instruction is given: just to run the graph."""
    return f"Run {arguments.graph}" + (f" on {arguments.branch}" if arguments.branch else "") + "."


def _inputs(pairs: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for pair in pairs:
        name, separator, value = pair.partition("=")
        if not separator or not name.strip():
            raise _UsageError(f"--input takes NAME=VALUE, not {pair!r}")
        values[name.strip()] = value
    return values


def _wait_for_run(client: Client, run_id: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout if timeout > 0 else None
    while True:
        run = client.get(f"/runs/{quote(run_id)}")
        if run["terminal"]:
            return run
        if run["status"] == "awaiting_approval" and run.get("pendingApprovals"):
            first = run["pendingApprovals"][0]
            print(f"waiting on approval {first['approvalId']} at {first['node']}: {first['reason']}",
                  file=sys.stderr)
        if deadline is not None and time.monotonic() >= deadline:
            print(f"oe: still {run['status']} after {timeout:g}s", file=sys.stderr)
            return run
        time.sleep(WAIT_INTERVAL_SECONDS)


def _wait_for_steering(client: Client, execution_id: str, steering_id: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout if timeout > 0 else None
    while True:
        node = client.get(f"/nodes/{quote(execution_id)}")
        steering = next(item for item in node["steering"] if item["steeringId"] == steering_id)
        if steering["status"] in TERMINAL_STEERING:
            return steering
        if deadline is not None and time.monotonic() >= deadline:
            return steering
        time.sleep(WAIT_INTERVAL_SECONDS)


def _run_failed(run: dict[str, Any]) -> bool:
    return run["status"] in {"failed", "cancelled"}


def _show_run(run: dict[str, Any], backend: Backend, pretty: bool) -> int:
    auth = (run.get("failure") or {}).get("authRequired")
    if auth and backend.name != backends.LOCAL:
        auth["command"] = f"{auth['command']} --backend {backend.name}"
    if pretty:
        if context := run.get("forgeContext"):
            print(context["summary"])
        print(f"{run['runId']}  {run['graph']} v{run['version']}  {run['status']}")
        if run.get("nodes"):
            _table(run["nodes"], _NODE_COLUMNS)
        for node, value in (run.get("results") or {}).items():
            print(f"\n[{node}]\n{value}")
        cost = run["usage"].get("costUsd")
        print(f"\nusage: {'unknown' if cost is None else f'${cost:.2f}'}"
              + ("" if run["usage"].get("complete", True) else " (some costs unknown)"))
        for pull in run.get("pullRequests") or []:
            print(f"pull request: {pull['repository']}#{pull['number']}")
    else:
        _print(run)
    failure = run.get("failure")
    if failure:
        print(f"oe: run failed{' at ' + failure['node'] if failure.get('node') else ''}: {failure['error']}",
              file=sys.stderr)
        if auth:
            print(f"oe: {auth['runner'] or 'a runner'} is not signed in; run `{auth['command']}`",
                  file=sys.stderr)
    return EXIT_FAILED if _run_failed(run) else EXIT_OK


def _health(backend: Backend) -> str:
    try:
        info = Client(backend, timeout=3.0).get("/backend")
    except RequestFailed as error:
        return f"down: {error}"
    return "up (" + ", ".join(info.get("runners", [])) + ")"


def _report(error: Exception) -> None:
    print(f"oe: {error}", file=sys.stderr)
    payload = getattr(error, "payload", None) or {}
    for candidate in payload.get("candidates") or []:
        print(f"  {json.dumps(candidate)}", file=sys.stderr)


def _print(payload: object) -> None:
    print(json.dumps(payload))


def _emit(payload: dict[str, Any], pretty: bool, line: Any) -> None:
    print(line(payload) if pretty else json.dumps(payload))


def _table(rows: list[dict[str, Any]], columns: list[tuple[str, Any]]) -> None:
    def cell(row: dict[str, Any], key: Any) -> str:
        value = key(row) if callable(key) else row.get(key)
        return "-" if value is None or value == "" else str(value)

    cells = [[cell(row, key) for _, key in columns] for row in rows]
    widths = [max([len(header), *(len(row[index]) for row in cells)]) for index, (header, _) in enumerate(columns)]
    print("  ".join(header.ljust(width) for (header, _), width in zip(columns, widths)).rstrip())
    for row in cells:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())


def _backend_line(body: dict[str, Any]) -> str:
    return f"{'* ' if body['current'] else ''}{body['name']}  {body['url']}  project {body['project']}"


def _agent_line(body: dict[str, Any]) -> str:
    detail = " ".join(part for part in (body.get("model") or "", body.get("url") or "", _agent_limit(body)) if part)
    line = f"{body['name']}  {body['kind']}{'  ' + detail if detail else ''}{'  (built in)' if body.get('builtin') else ''}"
    return line if body.get("models") is None else f"{line}\n{_runner_detail(body)}"


def _runner_detail(body: dict[str, Any]) -> str:
    """A built-in agent's binary and version, and its model for each tier."""
    binary = body.get("path") or "the adapter's bundled binary"
    version = f" ({body['version']})" if body.get("version") else ""
    models = body.get("models") or {}
    width = max((len(tier) for tier in models), default=0)
    return "\n".join([f"{body['name']}  {binary}{version}", *(
        f"  {tier.ljust(width)}  {model or '(agent default)'}" for tier, model in sorted(models.items())
    )])


def _agent_limit(body: dict[str, Any]) -> str:
    """An added agent's token limits, `context` or `context/output`, or blank when it has none."""
    if not body.get("contextLimit"):
        return ""
    return f"{body['contextLimit']}" + (f"/{body['outputLimit']}" if body.get("outputLimit") else "")


def _loop_summary(loop: dict[str, Any]) -> str:
    lines = [
        f"{loop['name']} ({loop['loopId']})  {loop['state']}"
        + (f": {loop['pauseReason']}" if loop.get("pauseReason") else ""),
        f"graph {loop['graph']} v{loop['version']} every {_duration(loop['everySeconds'])}, next {loop['nextRunAt'] or '-'}",
        f"pull requests {loop['prCount']}/{loop['limits']['maxPrs'] or '∞'}, spend {_spend(loop)}",
    ]
    if loop.get("activeRunId"):
        lines.append(f"active run {loop['activeRunId']}")
    latest = loop.get("latestOutput")
    if latest:
        lines.append(f"\nlatest output: {latest['node']} of {latest['runId']}, finished {latest['finishedAt']}")
        lines.append(_output_text(latest["value"]))
    elif "latestOutput" in loop:
        lines.append("latest output: none yet")
    return "\n".join(lines)


def _output_text(value: object) -> str:
    """A node's result for reading: text as written, anything else as indented JSON."""
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return json.dumps(value, indent=2)
    blocks = []
    for key, item in value.items():
        if key == "runner":
            continue
        text = item if isinstance(item, str) else json.dumps(item, indent=2)
        blocks.append(f"{key}:\n{text}" if "\n" in text else f"{key}: {text}")
    return "\n\n".join(blocks)


def _spend(loop: dict[str, Any]) -> str:
    spent = loop["spend"]
    cap = loop["limits"]["maxSpendUsd"]
    text = f"${spent['usd']:.2f}" + (f"/${cap:.2f}" if cap is not None else "")
    if not spent["complete"]:
        text += " (+unknown; cap not enforceable)" if cap is not None else " (+unknown)"
    return text


def _run_spend(run: dict[str, Any]) -> str | None:
    cost = run["usage"]["costUsd"]
    return None if cost is None else f"${cost:.2f}" + ("" if run["usage"]["complete"] else " (+unknown)")


def _duration(seconds: int) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


_AGENT_COLUMNS: list[tuple[str, Any]] = [
    ("NAME", "name"), ("KIND", "kind"), ("MODEL", "model"), ("URL", "url"), ("LIMIT", _agent_limit),
    ("", lambda row: "built in" if row.get("builtin") else ""),
]
_NODE_COLUMNS: list[tuple[str, Any]] = [
    ("NODE", "node"), ("EXECUTION", "executionId"), ("ATTEMPT", "attempt"), ("STATUS", "status"),
    ("RUNNER", "runner"), ("STARTED", "startedAt"), ("FINISHED", "finishedAt"),
]
_STEERING_COLUMNS: list[tuple[str, Any]] = [
    ("#", "sequence"), ("STEERING", "steeringId"), ("STATUS", "status"), ("ACCEPTED", "acceptedAt"),
    ("DELIVERED", "deliveredAt"), ("APPLIED", "appliedAt"), ("MESSAGE", "message"),
]

_HANDLERS = {
    ("verb", "list"): verbs_list,
    ("verb", "set"): verb_set,
    ("go", None): go,
    ("repo", "add"): onboard_repository,
    ("repo", "trust"): repo_trust,
    ("repo", "untrust"): repo_trust,
    ("backend", "add"): backend_add,
    ("backend", "list"): backend_list,
    ("backend", "use"): backend_use,
    ("backend", "remove"): backend_remove,
    ("graph", "spec"): graph_spec,
    ("graph", "add"): graph_add,
    ("graph", "get"): graph_get,
    ("graph", "run"): graph_run,
    ("graph", "list"): graphs_list,
    ("run", "get"): run_get,
    ("run", "wait"): run_wait,
    ("run", "watch"): run_watch,
    ("run", "list"): runs_list,
    ("loop", "spec"): loop_spec,
    ("loop", "add"): loop_add,
    ("loop", "get"): loop_get,
    ("loop", "pause"): loop_pause,
    ("loop", "resume"): loop_resume,
    ("loop", "stop"): loop_stop,
    ("loop", "list"): loops_list,
    ("node", "list"): nodes_list,
    ("node", "get"): node_get,
    ("node", "steer"): node_steer,
    ("agent", "add"): agent_add,
    ("agent", "get"): agent_get,
    ("agent", "remove"): agent_remove,
    ("agent", "signin"): agent_signin,
    ("agent", "list"): agents_list,
    ("runner", "signin"): runner_signin,
}


__all__ = ["COMMANDS", "add_parsers", "main"]
