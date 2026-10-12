"""Host and browser Origin validation for the daemon's HTTP surface."""

from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


def _origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            return None
        return (
            parsed.scheme,
            parsed.hostname.lower(),
            parsed.port
            if parsed.port is not None
            else (443 if parsed.scheme == "https" else 80),
        )
    except ValueError:
        return None


class RequestSecurity:
    def __init__(self, public_url: str) -> None:
        # public_url may include a deployment path; only its authority is trusted.
        parsed = urlsplit(public_url)
        self.public_origin = (
            _origin(f"{parsed.scheme}://{parsed.netloc}") if public_url else None
        )
        self.hosts = _LOOPBACK | (
            {self.public_origin[1]} if self.public_origin else set()
        )

    def trusted_host(self, headers: Headers) -> bool:
        hosts = headers.getlist("host")
        authority = _origin(f"http://{hosts[0]}") if len(hosts) == 1 else None
        return authority is not None and authority[1] in self.hosts

    def is_local_request(self, request: Request) -> bool:
        if not self.trusted_host(request.headers):
            return False
        origin = request.headers.get("origin")
        if origin is None:
            # CLI and service clients omit Origin; Host is still validated.
            return True
        parsed = _origin(origin)
        if parsed is None:
            return False
        if parsed == self.public_origin:
            return True
        request_origin = _origin(f"{request.url.scheme}://{request.headers['host']}")
        return (
            parsed[1] in _LOOPBACK
            and request_origin is not None
            and parsed[2] == request_origin[2]
        )


class TrustedDaemonHostMiddleware:
    def __init__(self, app: ASGIApp, security: RequestSecurity) -> None:
        self.app = app
        self.security = security

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not self.security.trusted_host(
            Headers(scope=scope)
        ):
            await JSONResponse({"error": "forbidden"}, status_code=403)(
                scope, receive, send
            )
            return
        await self.app(scope, receive, send)
