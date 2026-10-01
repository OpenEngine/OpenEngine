"""Execute compiled tests in isolated, origin-guarded Playwright contexts."""

import argparse
import asyncio
import hashlib
import json
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from open_verify import __version__
from open_verify.artifacts import Artifacts
from open_verify.auth import AssistedLogin, LoginRequest
from open_verify.browser_session import BrowserSession
from open_verify.media import encode_video
from open_verify.test_codegen import render_test
from open_verify.test_spec import BrowserTest, TestResult
from open_verify.tools import LocalTools

Journey = Callable[[object], Awaitable[None]]
JourneyResult = tuple[str, str, list[Path], list[Path], list[str]]


async def execute_journey(
    journey: Journey,
    tools: LocalTools,
    *,
    timeout: float,
    capture_media: bool,
    on_result: Callable[[JourneyResult], None] | None = None,
) -> JourneyResult:
    status, detail = "blocked", "Test did not start."
    screenshots, videos, omissions = [], [], []
    video = None
    has_app_page = False

    def checkpoint():
        completed = status, detail, screenshots, videos, omissions
        if on_result is not None:
            on_result(completed)
        return completed

    try:
        async with asyncio.timeout(timeout):
            page = await tools.browser_page()
            video = page.video if capture_media else None
            await journey(page)
            status, detail = "passed", "All generated Playwright assertions passed."
    except AssertionError as exc:
        status, detail = "failed", str(exc)
    except Exception as exc:
        # Failed navigation, missing browser/dependencies, action/deadline timeouts
        # are inconclusive setup/execution failures, not proven product defects.
        status, detail = "blocked", f"{type(exc).__name__}: {exc}"
    finally:
        try:
            if capture_media and tools.page is not None and tools.page.url != "about:blank":
                has_app_page = True
                screenshot = tools.artifacts.path / "result.png"
                try:
                    await tools.page.screenshot(path=str(screenshot), timeout=5000)
                    screenshots.append(screenshot)
                    # Presentation time for the final state, not test synchronization.
                    await asyncio.sleep(0.5)
                except Exception as exc:
                    omissions.append(f"Screenshot omitted: {exc}")
        finally:
            # Context closure finalizes the recording before conversion.
            errors = await tools.close()
            if errors:
                status, detail = "blocked", detail + " Cleanup failed: " + "; ".join(errors)
                omissions.append("Video omitted because browser cleanup did not complete.")
    if capture_media:
        screenshots[:0] = sorted(tools.artifacts.path.rglob("checkpoint-*.png"))
    if capture_media and video is not None and not errors and has_app_page:
        pending = "Video omitted: encoding has not completed."
        omissions.append(pending)
        checkpoint()
        try:
            source = await video.path()
            destination = tools.artifacts.path / "result.mp4"
            reason = await encode_video(source, destination)
            if reason:
                omissions.append(reason)
            else:
                videos.append(destination)
        except (asyncio.CancelledError, KeyboardInterrupt):
            omissions.remove(pending)
            omissions.append("Video omitted: encoding was interrupted.")
            checkpoint()
            raise
        except Exception as exc:
            omissions.append(f"Video omitted: {exc}")
        omissions.remove(pending)
    elif capture_media and video is None:
        omissions.append("Video omitted: browser recording could not start.")
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
            "The browser extra includes an MP4 encoder; ffmpeg on PATH takes precedence.\n",
            encoding="utf-8",
        )
        execution = Artifacts(self.artifacts.path / "executions" / identity)
        tools = LocalTools(
            self.project,
            execution,
            allow_origins=self.allow_origins,
            headless=self.headless,
            record_video=capture_media,
            storage_state=(self.authentication.state if test.authenticated and self.authentication else None),
            trace_browser=not test.authenticated,
            browser_session=self.browser_session,
        )

        # Execute the exact bytes saved for review/replay, produced only by the
        # typed compiler (never accept arbitrary Python from the agent).
        async def journey(page):
            tools.check_url(test.url)
            if test.authenticated and (
                self.authentication is None
                or not await self.authentication.verify(page.context, test.url)
            ):
                raise ValueError("Authentication is missing or expired; run assisted_login and retry this case")
            namespace = {"__name__": "generated_journey"}
            exec(compile(source, str(path), "exec"), namespace)
            await namespace["test_change"](page, progress=getattr(self, "progress", print))

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
        authentication = AssistedLogin(Path.cwd(), allow_origins=args.allow_origin, browser_session=session)
        tools = LocalTools(
            Path.cwd(),
            artifacts,
            allow_origins=args.allow_origin,
            headless=not args.headed,
            record_video=True,
            storage_state=str(args.auth_state) if authenticated else None,
            trace_browser=not authenticated,
            browser_session=session,
        )

        async def checked_journey(page):
            entry_url = os.environ.get("OV_BASE_URL", url)
            tools.check_url(entry_url)
            if args.login and not await authentication.verify(page.context, entry_url):
                raise ValueError("Login session is missing or expired")
            await journey(page, entry_url=entry_url)

        result = {}

        def checkpoint(completed: JourneyResult):
            status, detail, screenshots, videos, omissions = completed
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
                await authentication.run(LoginRequest.model_validate(login))
                tools.storage_state = authentication.state
            await execute_journey(
                checked_journey,
                tools,
                timeout=timeout,
                capture_media=True,
                on_result=checkpoint,
            )
        finally:
            errors = await session.close()
            if errors:
                raise RuntimeError("Replay browser cleanup failed: " + "; ".join(errors))
        print(json.dumps(result, indent=2))
        return {"passed": 0, "failed": 1, "blocked": 2}[result["status"]]

    sys.exit(asyncio.run(run()))
