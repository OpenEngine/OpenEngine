"""JSON over HTTP to a backend's `/api/v1` surface, retried only where that is safe.

A read is retried on a dropped connection. A write is retried only when it
carries an idempotency key, because only then can the backend tell a retry
from a second request: `graph run` and `node steer` always send one.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from engine.cli.backends import Backend

API_PREFIX = "/api/v1"
RETRY_DELAYS = (0.5, 1.0, 2.0)


class RequestFailed(RuntimeError):
    """A refusal or an unreachable backend, with whatever the backend said."""

    def __init__(self, message: str, *, status: int | None = None, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload or {}


@dataclass(frozen=True)
class Client:
    backend: Backend
    timeout: float = 30.0

    def get(self, path: str, **query: str | None) -> dict[str, Any]:
        return self.request("GET", path, query=query, retry=True)

    def post(self, path: str, body: dict[str, Any], *, idempotent: bool = False) -> dict[str, Any]:
        return self.request("POST", path, body=body, retry=idempotent)

    def delete(self, path: str) -> dict[str, Any]:
        return self.request("DELETE", path)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, str | None] | None = None,
        retry: bool = False,
    ) -> dict[str, Any]:
        parameters = {key: value for key, value in (query or {}).items() if value}
        url = f"{self.backend.url}{API_PREFIX}{path}" + (f"?{urlencode(parameters)}" if parameters else "")
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if token := self.backend.token():
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        attempts = (0.0, *RETRY_DELAYS) if retry else (0.0,)
        last: Exception | None = None
        for delay in attempts:
            if delay:
                time.sleep(delay)
            try:
                with urlopen(Request(url, data=data, method=method, headers=headers), timeout=self.timeout) as response:
                    payload = json.loads(response.read() or b"{}")
            except HTTPError as error:
                raise _refusal(self.backend, error) from None
            except (URLError, TimeoutError, ConnectionError) as error:
                last = error
                continue
            except ValueError:
                raise RequestFailed(f"{self.backend.name} did not answer with JSON") from None
            if not isinstance(payload, dict):
                raise RequestFailed(f"{self.backend.name} did not answer with a JSON object")
            return payload
        reason = getattr(last, "reason", last)
        raise RequestFailed(
            f"cannot reach backend {self.backend.name} at {self.backend.url}: {reason}"
            + ("" if self.backend.is_local else "; is its daemon running and reachable from here?")
        )


def _refusal(backend: Backend, error: HTTPError) -> RequestFailed:
    try:
        payload = json.loads(error.read())
    except (OSError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        payload = {}
    message = str(payload.get("error") or f"HTTP {error.code}")
    if error.code == 401:
        variable = backend.token_env or "ENGINE_SERVICE_TOKEN"
        message = f"backend {backend.name} requires a token; set {variable} (see `engine backend add --token-env`)"
    elif error.code == 404 and not payload:
        message = f"backend {backend.name} does not serve the graph API; upgrade its daemon"
    elif error.code == 503 and "graph" in message:
        message = f"backend {backend.name} is not running graph workflows: {message}"
    return RequestFailed(message, status=error.code, payload=payload)


__all__ = ["API_PREFIX", "Client", "RequestFailed"]
