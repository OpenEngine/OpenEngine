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
