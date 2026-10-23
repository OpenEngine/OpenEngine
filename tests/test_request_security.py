"""The daemon trusts configured authorities, never a client-selected Host."""

import pytest
from starlette.requests import Request

from engine.apps.web.request_security import RequestSecurity


@pytest.mark.parametrize(
    "host,origin,allowed",
    [
        ("localhost:4364", "http://localhost:4364", True),
        ("127.0.0.1:4364", "http://127.0.0.1:4364", True),
        ("[::1]:4364", "http://[::1]:4364", True),
        ("localhost:4364", None, True),
        ("engine.example:8443", "https://engine.example:8443", True),
        ("engine.example:8443", None, True),
        ("localhost:4364", "http://localhost:9999", False),
        ("engine.example:8443", "https://engine.example:9999", False),
        ("engine.example:8443", "http://engine.example:8443", False),
        ("evil.example", "http://evil.example", False),
        ("evil.example", None, False),
        ("localhost.evil.example", "http://localhost.evil.example", False),
        ("localhost", "http://[invalid", False),
        ("localhost", "http://localhost:invalid", False),
        ("localhost", "", False),
        ("localhost", "null", False),
        ("localhost@evil.example", None, False),
        ("localhost/path", None, False),
    ],
)
def test_trusted_authorities(host, origin, allowed):
    security = RequestSecurity("https://engine.example:8443/deployment")
    headers = [(b"host", host.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    request = Request(
        {"type": "http", "scheme": "http", "path": "/", "headers": headers}
    )
    assert security.is_local_request(request) is allowed


def test_unconfigured_public_host_and_duplicate_host_are_rejected():
    security = RequestSecurity("")
    for headers in [
        [(b"host", b"engine.example")],
        [(b"host", b"localhost"), (b"host", b"evil.example")],
        [],
    ]:
        request = Request(
            {"type": "http", "scheme": "http", "path": "/", "headers": headers}
        )
        assert not security.is_local_request(request)


@pytest.mark.parametrize("path", [
    "/api/github/events", "/api/slack/events",
    "/api/auth/github/callback", "/api/slack/callback",
])
def test_signature_and_oauth_endpoints_bypass_origin_check(path):
    import asyncio
    import httpx
    from starlette.responses import Response
    from engine.apps.web.request_security import TrustedDaemonHostMiddleware

    async def endpoint(scope, receive, send):
        await Response(status_code=204)(scope, receive, send)

    async def run():
        app = TrustedDaemonHostMiddleware(endpoint, RequestSecurity(""))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://localhost") as client:
            response = await client.post(path, content="signed payload", headers={
                "Origin": "https://evil.example", "Content-Type": "text/plain",
            })
            assert response.status_code == 204

    asyncio.run(run())


@pytest.mark.parametrize("origin", [None, "http://localhost:4364", "https://engine.example"])
@pytest.mark.parametrize("content_type,status", [(None, 415), ("", 415), ("text/plain", 415), ("application/json", 204), ("application/problem+json", 204), ("application/json; charset=utf-8", 204)])
def test_mutations_check_media_type_before_routing(origin, content_type, status):
    import asyncio
    import httpx
    from starlette.responses import Response
    from engine.apps.web.request_security import TrustedDaemonHostMiddleware

    async def endpoint(scope, receive, send):
        assert status == 204  # Refused bodies must never reach a handler.
        await Response(status_code=204)(scope, receive, send)

    async def run():
        app = TrustedDaemonHostMiddleware(endpoint, RequestSecurity("https://engine.example"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://localhost:4364") as client:
            headers = {} if content_type is None else {"Content-Type": content_type}
            if origin is not None:
                headers["Origin"] = origin
            assert (await client.post("/api/v1/graphs", content="{}", headers=headers)).status_code == status

    asyncio.run(run())


@pytest.mark.parametrize("content_type", [None, ""])
@pytest.mark.parametrize("body", [b"", b"{}"])
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_mutations_without_media_type_check_streamed_body(content_type, body, method):
    import asyncio
    import httpx
    from starlette.responses import Response
    from engine.apps.web.request_security import TrustedDaemonHostMiddleware

    async def endpoint(scope, receive, send):
        assert not body  # Nonempty bodies must be rejected before routing.
        assert await Request(scope, receive).body() == b""
        await Response(status_code=204)(scope, receive, send)

    async def stream():
        yield b""
        yield body

    async def run():
        app = TrustedDaemonHostMiddleware(endpoint, RequestSecurity(""))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://localhost") as client:
            headers = {} if content_type is None else {"Content-Type": content_type}
            response = await client.request(method, "/api/runs", content=stream(), headers=headers)
            assert response.status_code == (415 if body else 204)

    asyncio.run(run())
