"""Local QA adapters. All actions return observations recorded by the host."""

import asyncio
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
import psutil
from pydantic import Field

from open_verify.artifacts import Artifacts
from open_verify.browser_guard import BrowserGuard
from open_verify.browser_proxy import BrowserProxy
from open_verify.models import Contract
from open_verify.scope import OMIT, inspectable

READ_TOOLS = {"list_files", "read_file"}
MAX_TEXT = 24000
MAX_RESPONSE_FILE = 10 * 1024 * 1024


def project_root(path: Path) -> Path:
    path = path.resolve()
    if not path.is_dir():
        raise ValueError(f"Project directory does not exist: {path}")
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    raise ValueError(f"No Git project found at or above {path}")


class FileArgs(Contract):
    path: str = "."


class CommandArgs(Contract):
    argv: list[str] = Field(min_length=1)
    cwd: str = "."
    stdin: str = ""
    timeout: float = Field(default=30, gt=0, le=120)


class StartArgs(Contract):
    argv: list[str] = Field(min_length=1)
    cwd: str = "."


class ProcessArgs(Contract):
    process_id: str


class RequestArgs(Contract):
    url: str
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"] = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None
    timeout: float = Field(default=15, gt=0, le=60)


class OpenArgs(Contract):
    url: str


class EmptyArgs(Contract):
    pass


class WaitArgs(Contract):
    seconds: float = Field(default=60, gt=0, le=60)


class LocatorArgs(Contract):
    by: Literal["role", "label", "text", "test_id"]
    name: str
    role: str | None = None


class FillArgs(LocatorArgs):
    value: str


class PressArgs(LocatorArgs):
    key: str


class TextArgs(Contract):
    text: str


TOOLS = {
    "list_files": (FileArgs, "List up to 300 project files below a directory."),
    "read_file": (FileArgs, "Read up to 24000 characters of a UTF-8 project file."),
    "run_command": (
        CommandArgs,
        "Run an argv command with optional piped stdin; capture exit code and output.",
    ),
    "start_process": (
        StartArgs,
        "Start a background service; returns a managed process ID. Check readiness separately.",
    ),
    "process_output": (ProcessArgs, "Read current managed process output and exit code."),
    "stop_process": (ProcessArgs, "Stop a managed process and its descendants."),
    "wait": (WaitArgs, "Wait for a bounded number of seconds before a documented retry."),
    "http_request": (
        RequestArgs,
        "Send an HTTP request; return status, headers and body. Redirects are not followed.",
    ),
    "browser_open": (OpenArgs, "Open a URL in an isolated Chromium context."),
    "browser_snapshot": (
        EmptyArgs,
        "Observe current page accessibility tree and capture screenshot.",
    ),
    "browser_click": (
        LocatorArgs,
        "Click one observed element by accessible role/name, label, text or test ID.",
    ),
    "browser_fill": (FillArgs, "Fill an observed form field."),
    "browser_press": (PressArgs, "Press a key on an observed element, for example Enter."),
    "browser_expect_text": (
        TextArgs,
        "Wait up to 10 seconds for visible text, then capture the page.",
    ),
}


class LocalTools:
    def __init__(
        self,
        project: Path,
        artifacts: Artifacts,
        *,
        allow_exec=False,
        allow_origins=(),
        headless=False,
        record_video=False,
    ):
        self.project = project.resolve()
        self.artifacts = artifacts
        self.allow_exec = allow_exec
        self.origins = {self.origin(origin) for origin in allow_origins}
        self.headless = headless
        self.record_video = record_video
        self.processes: dict[str, tuple] = {}
        self.browser = self.context = self.page = self.playwright = None
        self._tracing = False
        self._browser_ready = False
        self._browser_guard = None
        self._browser_proxy = None
        self.browser_events: list[dict] = []
        self._http_response_count = 0

    def catalog(self, stage: str) -> dict:
        return {
            name: {"description": description, "arguments": schema.model_json_schema()}
            for name, (schema, description) in TOOLS.items()
            if stage != "discover" or name in READ_TOOLS
        }

    def path(self, name: str) -> Path:
        path = (self.project / name).resolve()
        if not path.is_relative_to(self.project):
            raise ValueError("Path is outside the target project")
        relative = path.relative_to(self.project)
        if any(part in OMIT for part in relative.parts):
            raise ValueError("Path is an excluded dependency or metadata directory")
        if not inspectable(relative.as_posix()):
            raise ValueError("Secret files are excluded from discovery")
        return path

    @staticmethod
    def origin(url: str) -> str:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Use an HTTP(S) URL without embedded credentials")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        host = parsed.hostname.lower()
        if ":" in host:
            host = f"[{host}]"
        return f"{parsed.scheme}://{host}:{port}"

    def check_url(self, url: str):
        origin = self.origin(url)
        if (
            urlsplit(url).hostname not in {"localhost", "127.0.0.1", "::1"}
            and origin not in self.origins
        ):
            raise ValueError("External origin requires --allow-origin: " + origin)

    def check_websocket_url(self, url: str):
        parsed = urlsplit(url)
        if parsed.scheme not in {"ws", "wss"}:
            raise ValueError("Use a WS(S) URL")
        # WebSockets share their HTTP(S) origin's allowance, including its port.
        self.check_url(
            parsed._replace(scheme={"ws": "http", "wss": "https"}[parsed.scheme]).geturl()
        )

    async def execute(self, name: str, arguments: dict, *, stage="execute") -> dict:
        try:
            if stage == "discover" and name not in READ_TOOLS:
                raise ValueError("Discovery permits only file listing and reading")
            if name not in TOOLS:
                raise ValueError(f"Unknown tool: {name}")
            args = TOOLS[name][0].model_validate(arguments)
            result = await getattr(self, name)(args)
            return self.artifacts.record(name, arguments, result, True)
        except Exception as exc:
            return self.artifacts.record(name, arguments, {"error": str(exc)}, False)

    async def list_files(self, args: FileArgs):
        root = self.path(args.path)
        if not root.is_dir():
            raise ValueError("Expected a directory")
        found = []
        for directory, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = sorted(
                d for d in dirs if d not in OMIT and not (Path(directory) / d).is_symlink()
            )
            for name in sorted(names):
                path = Path(directory) / name
                if path.is_symlink():
                    continue
                relative = path.relative_to(self.project).as_posix()
                try:
                    self.path(relative)
                except ValueError:
                    continue
                found.append(relative)
                if len(found) == 300:
                    return {"files": found, "truncated": True}
        return {"files": found, "truncated": False}

    async def read_file(self, args: FileArgs):
        with self.path(args.path).open(encoding="utf-8") as stream:
            content = stream.read(MAX_TEXT + 1)
        return {"text": content[:MAX_TEXT], "truncated": len(content) > MAX_TEXT}

    async def _spawn(self, argv, cwd, *, piped=False):
        if not self.allow_exec:
            raise ValueError("Command execution requires --allow-exec")
        process_id = f"P{len(self.processes) + 1:03d}"
        log = self.artifacts.path / f"{process_id}.log"
        with log.open("wb") as output:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self.path(cwd),
                stdout=output,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.PIPE if piped else asyncio.subprocess.DEVNULL,
                **(
                    {"creationflags": 0x08000000}
                    if os.name == "nt"
                    else {"start_new_session": True}
                ),
            )
        try:
            tracked = psutil.Process(process.pid)
        except psutil.NoSuchProcess:
            tracked = None  # A short-lived command may already have exited.
        self.processes[process_id] = (process, log, tracked, list(argv), cwd)
        return process_id, process

    async def run_command(self, args: CommandArgs):
        process_id, process = await self._spawn(args.argv, args.cwd, piped=True)
        timed_out = False
        try:
            await asyncio.wait_for(process.communicate(args.stdin.encode()), args.timeout)
        except TimeoutError:
            timed_out = True
            await self.stop_process(ProcessArgs(process_id=process_id))
        except BaseException:
            await self.stop_process(ProcessArgs(process_id=process_id))
            raise
        result = await self.process_output(ProcessArgs(process_id=process_id))
        return {**result, "timed_out": timed_out}

    async def start_process(self, args: StartArgs):
        process_id, _ = await self._spawn(args.argv, args.cwd)
        return {"process_id": process_id, "note": "Started; readiness has not been verified"}

    async def process_output(self, args: ProcessArgs):
        process, log, _, argv, cwd = self.processes[args.process_id]
        with log.open("rb") as stream:
            size = log.stat().st_size
            stream.seek(max(0, size - MAX_TEXT))
            content = stream.read(MAX_TEXT).decode("utf-8", errors="replace")
        return {
            "process_id": args.process_id,
            "exit_code": process.returncode,
            "output": content,
            "truncated": size > MAX_TEXT,
            "log": log.name,
            "argv": argv,
            "cwd": cwd,
        }

    async def stop_process(self, args: ProcessArgs):
        process, _, tracked, _, _ = self.processes[args.process_id]
        try:
            children = tracked.children(recursive=True) if tracked is not None else []
            targets = [*reversed(children), tracked] if tracked is not None else []
            for target in targets:
                try:
                    target.terminate()
                except psutil.NoSuchProcess:
                    pass
            _, alive = await asyncio.to_thread(psutil.wait_procs, targets, timeout=2)
            for target in alive:
                try:
                    target.kill()
                except psutil.NoSuchProcess:
                    pass
        except psutil.NoSuchProcess:
            pass
        await asyncio.wait_for(process.wait(), 5)
        return {"process_id": args.process_id, "exit_code": process.returncode}

    async def wait(self, args: WaitArgs):
        await asyncio.sleep(args.seconds)
        return {"seconds": args.seconds}

    async def http_request(self, args: RequestArgs):
        self.check_url(args.url)
        try:
            async with asyncio.timeout(args.timeout):
                return await self._http_request(args)
        except TimeoutError as exc:
            raise TimeoutError(
                f"HTTP action exceeded its {args.timeout:g}s overall deadline"
            ) from exc

    async def _http_request(self, args: RequestArgs):
        async with httpx.AsyncClient(
            follow_redirects=False, trust_env=False, timeout=args.timeout
        ) as client:
            async with client.stream(
                args.method, args.url, headers=args.headers, content=args.body
            ) as response:
                body = bytearray()
                self._http_response_count += 1
                relative = Path("responses") / f"http-{self._http_response_count:03d}.body"
                response_file = self.artifacts.path / relative
                response_file.parent.mkdir(exist_ok=True)
                size = 0
                complete = True
                with response_file.open("wb") as stream:
                    async for chunk in response.aiter_bytes():
                        remaining = MAX_RESPONSE_FILE - size
                        if remaining <= 0:
                            complete = False
                            break
                        saved = chunk[:remaining]
                        stream.write(saved)
                        size += len(saved)
                        if len(saved) != len(chunk):
                            complete = False
                            break
                        if len(body) <= MAX_TEXT:
                            body.extend(chunk[: MAX_TEXT + 1 - len(body)])
                return {
                    "status": response.status_code,
                    "headers": dict(response.headers),
                    "body": body[:MAX_TEXT].decode("utf-8", errors="replace"),
                    "truncated": size > MAX_TEXT,
                    "body_file": relative.as_posix(),
                    "body_file_complete": complete,
                }

    async def _ensure_browser(self):
        if self._browser_ready:
            return
        # Retain ownership after cleanup errors; never overwrite a live driver.
        errors = await self._close_browser()
        if errors:
            raise RuntimeError("Cannot retry browser initialization: " + "; ".join(errors))
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Install open-verify[browser], then run: playwright install chromium"
            ) from exc
        try:
            self._browser_proxy = BrowserProxy(self.check_url, self.browser_events)
            proxy_url = await self._browser_proxy.start()
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=self.headless,
                proxy={"server": proxy_url, "bypass": "<-loopback>"},
            )
            video_options = (
                {
                    "record_video_dir": str(self.artifacts.path / "raw-video"),
                    "record_video_size": {"width": 1280, "height": 720},
                }
                if self.record_video else {}
            )
            self.context = await self.browser.new_context(
                service_workers="block", viewport={"width": 1280, "height": 720}, **video_options
            )
            await self._configure_browser()
            self._browser_ready = True
        except BaseException as exc:
            # Includes cancellation during launch, context creation or tracing.
            errors = await self._close_browser()
            if errors:
                exc.add_note("Browser cleanup: " + "; ".join(errors))
            raise

    async def _configure_browser(self):

        async def route(request_route):
            try:
                self.check_url(request_route.request.url)
            except ValueError:
                self.browser_events.append({"blocked_url": request_route.request.url})
                await request_route.abort()
            else:
                await request_route.continue_()

        await self.context.route("**/*", route)

        async def websocket_route(websocket):
            try:
                self.check_websocket_url(websocket.url)
            except ValueError:
                self.browser_events.append({"blocked_websocket_url": websocket.url})
                await websocket.close(code=1008, reason="Origin not allowed")
            else:
                # No server connection exists until this explicit call.
                websocket.connect_to_server()

        await self.context.route_web_socket("**/*", websocket_route)
        await self.context.tracing.start(screenshots=True, snapshots=True)
        self._tracing = True
        self.page = await self.context.new_page()
        # CDP checks page/frame redirects. The proxy is installed before launch
        # so workers are covered even when Playwright resumes them first.
        cdp = await self.context.new_cdp_session(self.page)
        self._browser_guard = BrowserGuard(self.check_url, self.browser_events, self.context)
        await self._browser_guard.install(cdp)
        self.page.set_default_timeout(10000)
        self.page.on(
            "console",
            lambda message: self.browser_events.append(
                {"console": message.type, "text": message.text[:2000]}
            ),
        )
        self.page.on(
            "pageerror", lambda error: self.browser_events.append({"pageerror": str(error)})
        )

    async def browser_open(self, args: OpenArgs):
        self.check_url(args.url)
        await self._ensure_browser()
        await self.page.goto(args.url, wait_until="domcontentloaded", timeout=30000)
        return await self.browser_snapshot(EmptyArgs())

    async def browser_page(self):
        """Provide a page with the same network policy to a test runner adapter."""
        await self._ensure_browser()
        return self.page

    async def browser_snapshot(self, args: EmptyArgs):
        if self.page is None:
            raise ValueError("Open a browser page first")
        name = f"browser-{len(self.artifacts.observations) + 1:04d}.png"
        await self.page.screenshot(path=str(self.artifacts.path / name), full_page=True)
        snapshot = await self.page.locator("body").aria_snapshot()
        events = self.browser_events[-30:]
        self.browser_events.clear()
        return {
            "url": self.page.url,
            "snapshot": snapshot[:MAX_TEXT],
            "truncated": len(snapshot) > MAX_TEXT,
            "screenshot": name,
            "events": events,
        }

    def _locator(self, args: LocatorArgs):
        if self.page is None:
            raise ValueError("Open a browser page first")
        if args.by == "role":
            if not args.role:
                raise ValueError("A role locator requires role and name")
            return self.page.get_by_role(args.role, name=args.name, exact=True)
        if args.by == "test_id":
            return self.page.get_by_test_id(args.name)
        return getattr(self.page, f"get_by_{args.by}")(args.name, exact=True)

    async def browser_click(self, args: LocatorArgs):
        await self._locator(args).click()
        return await self.browser_snapshot(EmptyArgs())

    async def browser_fill(self, args: FillArgs):
        await self._locator(args).fill(args.value)
        return await self.browser_snapshot(EmptyArgs())

    async def browser_press(self, args: PressArgs):
        await self._locator(args).press(args.key)
        return await self.browser_snapshot(EmptyArgs())

    async def browser_expect_text(self, args: TextArgs):
        if self.page is None:
            raise ValueError("Open a browser page first")
        from playwright.async_api import expect

        await expect(self.page.get_by_text(args.text, exact=True)).to_be_visible()
        return await self.browser_snapshot(EmptyArgs())

    async def close(self):
        errors = []
        for process_id in self.processes:
            try:
                await self.stop_process(ProcessArgs(process_id=process_id))
            except Exception as exc:
                errors.append(f"{process_id}: {exc}")
        errors.extend(await self._close_browser())
        return errors

    async def _close_browser(self):
        errors = []
        self._browser_ready = False
        if self._browser_guard is not None:
            await self._browser_guard.close()
            self._browser_guard = None
        for name, operation in [
            (
                "trace",
                lambda: (
                    self.context.tracing.stop(path=str(self.artifacts.path / "browser-trace.zip"))
                    if self.context and self._tracing
                    else None
                ),
            ),
            ("context", lambda: self.context.close() if self.context else None),
            ("browser", lambda: self.browser.close() if self.browser else None),
            ("playwright", lambda: self.playwright.stop() if self.playwright else None),
            ("proxy", lambda: self._browser_proxy.close() if self._browser_proxy else None),
        ]:
            try:
                pending = operation()
                if pending is not None:
                    await pending
                if name == "trace":
                    self._tracing = False
                elif name == "context":
                    self.context = self.page = None
                elif name == "browser":
                    self.browser = self.context = self.page = None
                elif name == "playwright":
                    self.playwright = self.browser = self.context = self.page = None
                    self._tracing = False
                elif name == "proxy":
                    self._browser_proxy = None
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        return errors
