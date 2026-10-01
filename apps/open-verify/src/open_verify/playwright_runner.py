"""Execute compiled tests in isolated, origin-guarded Playwright contexts."""

import argparse
import asyncio
import hashlib
import inspect
import json
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from open_verify import __version__
from open_verify.artifacts import Artifacts
from open_verify.auth import AssistedLogin, LoginRequest
from open_verify.browser_session import BrowserSession
from open_verify.capture import CheckpointCapture
from open_verify.media import encode_gif
from open_verify.test_codegen import render_test
from open_verify.test_spec import BrowserTest, TestResult
from open_verify.tools import LocalTools

Journey = Callable[[object, CheckpointCapture | None], Awaitable[None]]
JourneyResult = tuple[str, str, list[Path], list[Path], list[str]]


def unique_screenshots(paths):
    previous = None
    result = []
    for path in paths:
        digest = hashlib.sha256(path.read_bytes()).digest()
        if digest != previous:
            previous = digest
            result.append(path)
    return result


async def execute_journey(
    journey: Journey,
    tools: LocalTools,
    *,
    timeout: float,
    capture_media: bool,
    on_result: Callable[[JourneyResult], None] | None = None,
    prefix_screenshots: tuple[Path, ...] = (),
) -> JourneyResult:
    status, detail = "blocked", "Test did not start."
    screenshots, videos, omissions = [], [], []
    capture = CheckpointCapture(tools.artifacts.path / 'checkpoints') if capture_media else None
    has_app_page = False

    def checkpoint():
        completed = status, detail, screenshots, videos, omissions
        if on_result is not None:
            on_result(completed)
        return completed

    try:
        async with asyncio.timeout(timeout):
            page = await tools.browser_page()
            await journey(page, capture)
            status, detail = "passed", "All generated Playwright assertions passed."
    except AssertionError as exc:
        status, detail = "failed", str(exc)
    except Exception as exc:
        status, detail = "blocked", f"{type(exc).__name__}: {exc}"
    finally:
        try:
            if capture is not None and tools.page is not None and tools.page.url != "about:blank":
                has_app_page = True
                await capture(tools.page, 'result')
        finally:
            errors = await tools.close()
            if errors:
                status, detail = "blocked", detail + " Cleanup failed: " + "; ".join(errors)
                omissions.append("GIF omitted because browser cleanup did not complete.")
    if capture is not None:
        screenshots = unique_screenshots([*prefix_screenshots, *capture.paths])
        omissions.extend(capture.omissions)
    if capture is not None and not errors and has_app_page:
        pending = "GIF omitted: encoding has not completed."
        omissions.append(pending)
        checkpoint()
        try:
            destination = tools.artifacts.path / "journey-summary.gif"
            reason = await encode_gif(screenshots, destination)
            if reason:
                omissions.append(reason)
            else:
                screenshots.append(destination)
        except (asyncio.CancelledError, KeyboardInterrupt):
            omissions.remove(pending)
            omissions.append("GIF omitted: encoding was interrupted.")
            checkpoint()
            raise
        except Exception as exc:
            omissions.append(f"GIF omitted: {exc}")
        omissions.remove(pending)
    elif capture_media and not has_app_page:
        omissions.append("Visual evidence omitted: no application page was reached.")
    return checkpoint()


class PlaywrightRunner:
    def __init__(self, project: Path, artifacts: Artifacts, *, allow_origins=(), headless=True,
                 browser_session=None):
        self.project = project
        self.artifacts = artifacts
        self.allow_origins = tuple(allow_origins)
        self.headless = headless
        self.attempt = 0
        self.authentication = None
        self.browser_session = browser_session

    async def run(
        self,
        test: BrowserTest,
        *,
        capture_media: bool,
        on_result: Callable[[TestResult], None] | None = None,
    ) -> TestResult:
        self.attempt += 1
        identity = hashlib.sha256(test.case_id.encode()).hexdigest()[:16]
        folder = self.artifacts.path / "tests"
        folder.mkdir(exist_ok=True)
        path = folder / f"test_{identity}_{self.attempt:03d}.py"
        login_request = (self.authentication.request.model_dump()
                         if test.authenticated and self.authentication and self.authentication.request else None)
        source = render_test(test, login=login_request)
        path.write_text(source, encoding="utf-8")
        path.with_suffix(".json").write_text(test.model_dump_json(indent=2), encoding="utf-8")
        (folder / "requirements.txt").write_text(
            f"open-verify[browser]=={__version__}\n", encoding="utf-8"
        )
        (folder / "README.md").write_text(
            "# Generated Playwright tests\n\n"
            "Use the same Open Verify version (install from its standalone source if unpublished), "
            "install the browser extra, then run `playwright install chromium`. "
            "Start the app using ../plan.json and the recorded prerequisites in ../report.md. "
            "Each test starts with an isolated browser context. Authenticated tests use "
            "--login for assisted sign-in or a private --auth-state file for replay; other tests start signed out. Prerequisite "
            "UI steps belong in the test. Use disposable test data. No dependencies are installed automatically.\n\n"
            "Run the manifest's argv from the bundle root. Set OV_BASE_URL to override the entry "
            "URL; explicit URL assertions still check their recorded values. The test_change(page) "
            "function uses ordinary Playwright and can also be adopted into an async test suite. "
            "The standalone entry point keeps Open Verify's origin guards. "
            "The browser extra includes a GIF encoder; ffmpeg on PATH takes precedence.\n",
            encoding="utf-8",
        )
        execution = Artifacts(self.artifacts.path / "executions" / identity)
        tools = LocalTools(
            self.project,
            execution,
            allow_origins=self.allow_origins,
            headless=self.headless,
            record_video=False,
            storage_state=(self.authentication.state if test.authenticated and self.authentication else None),
            trace_browser=not test.authenticated,
            browser_session=self.browser_session,
        )

        # Execute the exact bytes saved for review/replay, produced only by the
        # typed compiler (never accept arbitrary Python from the agent).
        async def journey(page, capture):
            tools.check_url(test.url)
            if test.authenticated and (
                self.authentication is None
                or not await self.authentication.verify(page.context, test.url)
            ):
                raise ValueError("Authentication is missing or expired; run assisted_login and retry this case")
            namespace = {"__name__": "generated_journey"}
            exec(compile(source, str(path), "exec"), namespace)
            await namespace["test_change"](page, progress=getattr(self, "progress", print), capture=capture)

        relative = path.relative_to(self.artifacts.path).as_posix()
        rerun = ["python", relative]
        if login_request:
            rerun.append("--login")
        for origin in self.allow_origins:
            rerun.extend(["--allow-origin", origin])
        result: TestResult

        def checkpoint(completed: JourneyResult):
            nonlocal result
            status, detail, screenshots, videos, omissions = completed
            if test.authenticated and self.authentication is not None:
                if self.authentication.summary:
                    detail = self.authentication.summary + " " + detail
                if capture_media:
                    omissions = self.authentication.omissions + omissions
            screenshots = unique_screenshots(screenshots)
            result = TestResult(
                case_id=test.case_id,
                status=status,
                detail=detail,
                test_file=relative,
                rerun=rerun,
                screenshots=[p.relative_to(self.artifacts.path).as_posix() for p in screenshots],
                videos=[p.relative_to(self.artifacts.path).as_posix() for p in videos],
                omissions=omissions,
            )
            execution.write("result.json", result.model_dump())
            if on_result is not None:
                on_result(result)

        await execute_journey(
            journey,
            tools,
            timeout=test.timeout,
            capture_media=capture_media,
            on_result=checkpoint,
            prefix_screenshots=tuple(self.artifacts.path / p for p in self.authentication.screenshots if p.endswith('.png'))
                if test.authenticated and self.authentication else (),
        )
        return result


def replay_main(journey, *, url: str, timeout: float, authenticated=False, login=None):
    """Standalone harness used by generated files; no provider or Git needed."""
    cli = argparse.ArgumentParser(description="Replay a generated Playwright journey")
    cli.add_argument("--allow-origin", action="append", default=[])
    cli.add_argument("--headed", action="store_true")
    auth = cli.add_mutually_exclusive_group()
    auth.add_argument("--auth-state", type=Path, help="Private Playwright storage-state file for authenticated replay")
    auth.add_argument("--login", action="store_true", help="Sign in interactively before replaying this journey")
    cli.add_argument("--output", type=Path, default=Path.cwd() / "verification-replay")
    args = cli.parse_args()
    if authenticated and args.auth_state is None and not args.login:
        cli.error("This journey requires --login or --auth-state; no session is included in the bundle")
    if args.login and (not login or not sys.stdin.isatty()):
        cli.error("--login requires saved sign-in instructions and an interactive terminal")

    async def run():
        artifacts = Artifacts(args.output.resolve())
        session = BrowserSession(headless=not (args.headed or args.login))
        authentication = AssistedLogin(Path.cwd(), allow_origins=args.allow_origin, browser_session=session,
                                       artifacts=artifacts)
        tools = LocalTools(
            Path.cwd(),
            artifacts,
            allow_origins=args.allow_origin,
            headless=not args.headed,
            record_video=False,
            storage_state=str(args.auth_state) if authenticated else None,
            trace_browser=not authenticated,
            browser_session=session,
        )

        async def checked_journey(page, capture):
            entry_url = os.environ.get("OV_BASE_URL", url)
            tools.check_url(entry_url)
            if args.login and not await authentication.verify(page.context, entry_url):
                raise ValueError("Login session is missing or expired")
            kwargs = {"entry_url": entry_url}
            if 'capture' in inspect.signature(journey).parameters:
                kwargs['capture'] = capture
            await journey(page, **kwargs)

        result = {}

        def checkpoint(completed: JourneyResult):
            status, detail, screenshots, videos, omissions = completed
            if args.login:
                detail = authentication.summary + " " + detail
                omissions = authentication.omissions + omissions
            screenshots = unique_screenshots(screenshots)
            result.update(
                status=status,
                detail=detail,
                screenshots=list(map(str, screenshots)),
                videos=list(map(str, videos)),
                omissions=list(omissions),
            )
            artifacts.write("result.json", result)

        try:
            if args.login:
                # Login URLs are explicit, recorded setup; an OV_BASE_URL override
                # never silently sends credentials to a different application.
                await authentication.run(LoginRequest.model_validate(login), capture_media=True)
                tools.storage_state = authentication.state
            await execute_journey(
                checked_journey,
                tools,
                timeout=timeout,
                capture_media=True,
                on_result=checkpoint,
                prefix_screenshots=tuple(artifacts.path / p for p in authentication.screenshots if p.endswith('.png'))
                    if args.login else (),
            )
        finally:
            errors = await session.close()
            if errors:
                raise RuntimeError("Replay browser cleanup failed: " + "; ".join(errors))
        print(json.dumps(result, indent=2))
        return {"passed": 0, "failed": 1, "blocked": 2}[result["status"]]

    sys.exit(asyncio.run(run()))
