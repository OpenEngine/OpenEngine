"""Authenticated transports for the GitHub REST API.

The source-control adapter owns GitHub resource semantics and error messages.
These transports only execute a request using either an OAuth bearer token or
the credentials already managed by ``gh auth``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

import httpx


#: How long a `gh` that has been asked to stop is given before it is killed.
_TERMINATION_GRACE_SECONDS = 5


class GitHubTransportError(RuntimeError):
    """One transport could not complete a GitHub API request."""


class GitHubApiTransport(Protocol):
    async def request(self, method: str, path: str, **kwargs: object) -> object: ...

    async def download(self, path: str) -> bytes: ...


class GitHubOAuthTransport:
    """GitHub REST transport authenticated by a token supplier."""

    def __init__(
        self,
        token: str | Callable[[], str | None],
        api_url: str = "https://api.github.com",
        on_token_unauthorized: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self._token_source = token
        self._api_url = api_url.rstrip("/")
        self._on_token_unauthorized = on_token_unauthorized

    @property
    def _token(self) -> str:
        return (
            self._token_source() or ""
            if callable(self._token_source)
            else self._token_source
        )

    def _headers(self, token: str) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def request(self, method: str, path: str, **kwargs: object) -> object:
        token = self._token
        async with httpx.AsyncClient() as client:
            response = await client.request(
                method, f"{self._api_url}{path}", headers=self._headers(token), **kwargs
            )
        if await self._refresh_after_unauthorized(response, token):
            async with httpx.AsyncClient() as client:
                response = await client.request(
                    method,
                    f"{self._api_url}{path}",
                    headers=self._headers(self._token),
                    **kwargs,
                )
        if response.is_error:
            raise self._request_error(method, path, response)
        if response.status_code == 204 or not response.content:
            return {}
        try:
            return response.json()
        except ValueError as error:
            raise GitHubTransportError(
                f"GitHub API {method} {path} returned invalid JSON"
            ) from error

    async def download(self, path: str) -> bytes:
        token = self._token
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(
                f"{self._api_url}{path}", headers=self._headers(token)
            )
        if await self._refresh_after_unauthorized(response, token):
            async with httpx.AsyncClient(follow_redirects=True) as client:
                response = await client.get(
                    f"{self._api_url}{path}", headers=self._headers(self._token)
                )
        if response.is_error:
            raise self._request_error("GET", path, response)
        return response.content

    async def _refresh_after_unauthorized(
        self, response: httpx.Response, failed_token: str
    ) -> bool:
        """Refresh once after a 401, leaving other HTTP errors untouched."""
        return bool(
            response.status_code == 401
            and failed_token
            and self._on_token_unauthorized is not None
            and await self._on_token_unauthorized(failed_token)
        )

    @staticmethod
    def _request_error(
        method: str, path: str, response: httpx.Response
    ) -> GitHubTransportError:
        try:
            detail = response.json().get("message", response.text)
        except ValueError:
            detail = response.text or f"HTTP {response.status_code}"
        return GitHubTransportError(
            f"GitHub API {method} {path} failed ({response.status_code}): {detail}"
        )


#: How long one ``gh api`` call may take before it is abandoned. The OAuth
#: transport is bounded by httpx's own default; this one shells out, so it is
#: bounded here or not at all -- and an unbounded API call is not merely slow,
#: because callers are serialized behind a single worker in places (the GitHub
#: webhook queue among them), where one stalled process stops all of them.
#: Generous next to a REST call GitHub answers in well under a second.
CLI_TIMEOUT_SECONDS = 30


class GitHubCliTransport:
    """GitHub REST transport delegated to the user's authenticated ``gh`` CLI."""

    def __init__(
        self, binary_path: str = "gh", timeout_seconds: float = CLI_TIMEOUT_SECONDS
    ) -> None:
        self._binary_path = binary_path
        self._timeout_seconds = timeout_seconds

    async def request(self, method: str, path: str, **kwargs: object) -> object:
        arguments = [
            "api",
            path,
            "--method",
            method,
            "--header",
            "Accept: application/vnd.github+json",
            "--header",
            "X-GitHub-Api-Version: 2022-11-28",
        ]
        params = kwargs.get("params")
        if isinstance(params, Mapping):
            for key, value in params.items():
                arguments.extend(["--raw-field", f"{key}={value}"])
        body: bytes | None = None
        payload = kwargs.get("json")
        if payload is not None:
            arguments.extend(["--input", "-"])
            body = json.dumps(payload).encode()
        output = await self._run(*arguments, input_bytes=body)
        if not output.strip():
            return {}
        try:
            return json.loads(output)
        except json.JSONDecodeError as error:
            raise GitHubTransportError("gh returned a non-JSON API response") from error

    async def download(self, path: str) -> bytes:
        return await self._run("api", path, "--method", "GET")

    async def _run(self, *arguments: str, input_bytes: bytes | None = None) -> bytes:
        try:
            process = await asyncio.create_subprocess_exec(
                self._binary_path,
                *arguments,
                stdin=asyncio.subprocess.PIPE if input_bytes is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as error:
            raise GitHubTransportError(
                "GitHub CLI is not installed; install it and run 'gh auth login', "
                "or select GitHub OAuth in Settings"
            ) from error
        except OSError as error:
            raise GitHubTransportError(
                f"could not start {self._binary_path}: {error}"
            ) from error
        try:
            async with asyncio.timeout(self._timeout_seconds):
                stdout, stderr = await process.communicate(input_bytes)
        except TimeoutError as error:
            # `communicate` is cancelled by the timeout, but the process it was
            # reading is not: without this, an abandoned `gh` keeps the pipes
            # open and stays a child of this process forever.
            await self._terminate(process)
            raise GitHubTransportError(
                f"gh API request timed out after {self._timeout_seconds:g}s"
            ) from error
        if process.returncode:
            detail = stderr.decode(errors="replace").strip()
            if "not logged into" in detail.lower() or "authenticate" in detail.lower():
                detail = (
                    "GitHub CLI is not authenticated; run 'gh auth login' and try again"
                )
            raise GitHubTransportError(detail or "gh API request failed")
        return stdout

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        """End an abandoned `gh`, escalating once if it ignores the first ask.

        Reaped either way: an un-awaited process stays a zombie, and a caller
        that timed out is not waiting for this.
        """
        if process.returncode is not None:
            return
        for stop in (process.terminate, process.kill):
            try:
                stop()
            except ProcessLookupError:
                return
            try:
                async with asyncio.timeout(_TERMINATION_GRACE_SECONDS):
                    await process.wait()
                return
            except TimeoutError:
                continue


__all__ = [
    "CLI_TIMEOUT_SECONDS",
    "GitHubApiTransport",
    "GitHubCliTransport",
    "GitHubOAuthTransport",
    "GitHubTransportError",
]
