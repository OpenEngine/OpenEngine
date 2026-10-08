"""The terminal is Open Verify's only user interface."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from platformdirs import user_cache_path, user_data_path

from open_verify import __version__
from open_verify.agent import ACPDecisionAgent, provider_for
from open_verify.artifacts import Artifacts
from open_verify.browser_session import BrowserSession
from open_verify.changes import read_change
from open_verify.local_engine import LocalEngine, project_root
from open_verify.manifest import write_manifest
from open_verify.playwright_runner import PlaywrightRunner
from open_verify.replay_cache import ReplayCache
from open_verify.runner import VerificationRunner, exit_code


class TerminalProgress:
    """Keep an interactive terminal's current background activity on one line."""

    def __init__(self, stream):
        self.stream = stream
        self.interactive = stream.isatty()
        self.active = False

    def status(self, message: str):
        if not self.interactive:
            return
        self.stream.write("\r\033[2K" + message)
        self.stream.flush()
        self.active = True

    def write(self, message: str):
        if self.active:
            self.stream.write("\r\033[2K")
            self.active = False
        print(message, file=self.stream, flush=True)

    def clear(self):
        if self.active:
            self.stream.write("\r\033[2K")
            self.stream.flush()
            self.active = False


def positive_int(value):
    number = int(value)
    if not 1 <= number <= 1000:
        raise argparse.ArgumentTypeError("must be between 1 and 1000")
    return number


def parser():
    cli = argparse.ArgumentParser(
        description="Verify a feature or Git change and export tests and evidence."
    )
    cli.add_argument("request", nargs="?", help="Feature and expected behavior to verify")
    cli.add_argument("--version", action="version", version=__version__)
    cli.add_argument(
        "--project", type=Path, default=Path.cwd(), help="Target directory (default: cwd)"
    )
    cli.add_argument(
        "--agent", default="codex", help="ACP provider: codex, claude, or a custom name"
    )
    cli.add_argument(
        "--agent-command", help='Custom ACP launch command as JSON, e.g. ["my-agent", "--acp"]'
    )
    cli.add_argument('--no-knowledge', action='store_true', help='Disable project onboarding knowledge')
    cli.add_argument('--refresh-knowledge', action='store_true', help='Gather additional product onboarding facts')
    cli.add_argument("--model", help="Provider-specific model ID; omission uses its default")
    cli.add_argument("--base", help="Base revision for change-based verification (no checkout/fetch)")
    cli.add_argument("--head", help="Changed revision, which must match the checkout (default: HEAD)")
    cli.add_argument("--pr", help="GitHub PR URL to fetch and verify in an isolated worktree")
    cli.add_argument("--publish", action="store_true", help="Upload PR test evidence and post a GitHub comment")
    cli.add_argument("--publish-from", type=Path, help="Publish a saved PR run directory without rerunning tests")
    cli.add_argument("--setup-file", action="append", default=[], help="Explicit local config file to copy into the PR worktree (relative to --project; repeatable)")
    cli.add_argument(
        "--include-working-tree", action="store_true",
        help="Include staged, unstaged, and untracked files in the change",
    )
    cli.add_argument(
        "--plan-only", action="store_true", help="Discover and plan without executing QA actions"
    )
    cli.add_argument(
        "--allow-exec",
        action="store_true",
        help="Allow local commands and project-local dependency installation (unless your request forbids it)",
    )
    cli.add_argument(
        "--allow-origin",
        action="append",
        default=[],
        help="Additional browser/HTTP origin; repeatable. Localhost is allowed.",
    )
    cli.add_argument("--cache", choices=["auto", "strict", "refresh", "off"], default="auto",
                     help="Action replay: auto reuse/fallback, strict read-only, refresh live recordings, or off")
    cli.add_argument("--cache-dir", type=Path, default=user_cache_path("open-verify") / "replay",
                     help="Local recording storage, isolated by project; excluded from run bundles")
    cli.add_argument(
        "--verification", choices=["live", "tests"], default="live",
        help="live: manual QA through app UI/API/CLI (default); tests: permit existing test suites",
    )
    cli.add_argument("--headless", action="store_true", help="Hide the browser window")
    cli.add_argument(
        "--max-steps", type=positive_int, default=60, help="Maximum agent decisions (default: 60)"
    )
    cli.add_argument("--max-cases", type=positive_int, default=1,
                     help="Maximum planned journeys (default: 1 application smoke; increase for supporting checks)")
    cli.add_argument(
        "--agent-timeout", type=positive_int, default=180, help="Seconds per agent decision"
    )
    cli.add_argument(
        "--output",
        type=Path,
        default=user_data_path("open-verify") / "runs",
        help="Parent directory for a fresh run's artifacts",
    )
    return cli


async def run(args):
    if args.publish_from:
        from open_verify.github import publish_saved
        return await publish_saved(args.publish_from)
    if args.pr:
        from open_verify.pull_request import run_pull_request
        return await run_pull_request(args, run_local)
    return await run_local(args)


async def run_local(args, *, artifacts=None, prepare=None):
    artifacts = artifacts or Artifacts(args.output.resolve())
    try:
        project = project_root(args.project)
        change = (
            await read_change(
                project, args.base, args.head or "HEAD",
                include_working_tree=args.include_working_tree,
            )
            if args.base else None
        )
        if prepare is not None:
            prepare()
    except Exception as exc:
        report = {"request": args.request, "status": "blocked", "findings": [],
                  "note": f"Change/project inspection failed: {exc}"}
        artifacts.report(report)
        write_manifest(artifacts.path, report, None, [])
        print(f"Open Verify: {report['note']}", file=sys.stderr)
        print(f"Manifest: {artifacts.path / 'manifest.json'}", flush=True)
        return 2
    command = json.loads(args.agent_command) if args.agent_command else None
    if command is not None and (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) and part for part in command)
    ):
        raise ValueError("--agent-command must be a nonempty JSON array of strings")
    provider = provider_for(args.agent, command)
    for origin in args.allow_origin:
        LocalEngine.origin(origin)
    artifacts.write(
        "session.json",
        {
            "project": str(project),
            "request": args.request,
            "agent": args.agent,
            "model": args.model,
            "plan_only": args.plan_only,
            "verification": args.verification,
            "cache": args.cache,
            "allow_exec": args.allow_exec,
            "setup_files": args.setup_file,
            "change": change.model_dump(exclude={"diff"}) if change else None,
        },
    )
    if change is not None:
        artifacts.write("change.json", change.model_dump())
    # Agent-side cwd is the run folder. Repository inspection and execution are
    # routed through host adapters, not the provider's native workspace tools.
    agent = ACPDecisionAgent(provider, artifacts.path, model=args.model, timeout=args.agent_timeout)
    browser_session = BrowserSession(headless=args.headless)
    tools = LocalEngine(
        project,
        artifacts,
        allow_exec=args.allow_exec,
        allow_origins=args.allow_origin,
        headless=args.headless,
        browser_session=browser_session,
    )
    print(f"Project: {project}\nArtifacts: {artifacts.path}", flush=True)
    terminal = TerminalProgress(sys.stdout)

    async def ask_user(question: str) -> str | None:
        if not sys.stdin.isatty():
            return None
        try:
            return await asyncio.to_thread(input, f"\nOpen Verify needs setup information:\n{question}\n> ")
        except (EOFError, KeyboardInterrupt):
            return None

    verification = VerificationRunner(
        agent,
        tools,
        artifacts,
        plan_only=args.plan_only,
        verification=args.verification,
        max_steps=args.max_steps,
        progress=terminal.write,
        progress_status=terminal.status,
        ask_user=ask_user,
        interactive_login=sys.stdin.isatty(),
        setup_files=args.setup_file,
        max_cases=args.max_cases,
        replay_cache=ReplayCache(args.cache_dir.resolve(), project) if args.cache != "off" else None,
        cache_mode=args.cache,
        change=change,
        knowledge_enabled=not getattr(args, 'no_knowledge', False),
        refresh_knowledge=getattr(args, 'refresh_knowledge', False),
        knowledge_path=getattr(args, 'knowledge_path', None),
        knowledge_identity=getattr(args, 'knowledge_identity', None),
        test_runner=(
            PlaywrightRunner(
                project, artifacts, allow_origins=args.allow_origin, headless=args.headless,
                browser_session=browser_session,
            ) if change is not None else None
        ),
    )
    cleanup_errors = []
    report = None
    try:
        report = await verification.run(args.request)
    finally:
        terminal.clear()
        cleanup_errors = await tools.close()
        cleanup_errors.extend(await browser_session.close())
        try:
            await agent.close()
        except Exception as exc:
            cleanup_errors.append(f"agent: {exc}")
        if cleanup_errors:
            artifacts.write("cleanup-errors.json", cleanup_errors)
            print("Cleanup needs attention: " + "; ".join(cleanup_errors), file=sys.stderr)
        # Refresh the publication contract after all resources have been closed,
        # including when the runner saved a partial report before raising.
        saved = artifacts.path / "report.json"
        final_report = report or (json.loads(saved.read_text(encoding="utf-8")) if saved.exists() else None)
        if final_report is not None:
            verification.publish(final_report, cleanup_errors=cleanup_errors)
    if report and (report.get('note') or report.get('status') in {'incomplete', 'blocked', 'interrupted'}):
        reason = ' '.join(str(report.get('note') or 'Verification did not complete.').split())
        print(f"Run {report['status']}: {reason}", flush=True)
    print(f"Report: {artifacts.path / 'report.md'}")
    print(f"Manifest: {artifacts.path / 'manifest.json'}")
    return 2 if cleanup_errors else exit_code(report)


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    if args.max_cases > 20:
        cli.error("--max-cases cannot exceed 20")
    if args.publish_from:
        if args.request or args.pr or args.publish or args.base or args.head or args.include_working_tree or args.setup_file or args.plan_only or args.allow_exec:
            cli.error("--publish-from publishes saved evidence; do not combine it with a verification request or execution options")
        args.request = "Publish saved evidence"
    if args.pr and (args.base or args.head or args.include_working_tree):
        cli.error("--pr resolves its own revisions; do not combine with --base, --head, or --include-working-tree")
    if (args.publish or args.setup_file) and not args.pr:
        cli.error("--publish and --setup-file require --pr")
    if args.publish and (args.plan_only or not args.allow_exec):
        cli.error("--publish requires --allow-exec and cannot be combined with --plan-only")
    if (args.head or args.include_working_tree) and not args.base:
        cli.error("--head and --include-working-tree require --base")
    if not args.request and (args.base or args.pr):
        args.request = "Verify the meaningful behavior affected by this change."
    if not args.request:
        if not sys.stdin.isatty():
            cli.error("provide a verification request when stdin is not interactive")
        try:
            args.request = input("What should Open Verify test? ").strip()
        except (EOFError, KeyboardInterrupt):
            return 130
    if not args.request.strip():
        cli.error("the verification request cannot be empty")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("Verification interrupted. Partial evidence is saved.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Open Verify: {exc}", file=sys.stderr)
        return 2
