import asyncio

import httpx
import pytest

from engine.apps.web.gitlab_auth import (
    GitLabAuthError,
    GitLabRefreshTokenInvalidError,
    normalize_origin,
    poll_device_flow,
    refresh_access_token,
    start_device_flow,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("gitlab.com", "https://gitlab.com"),
        ("HTTPS://GITLAB.COM/", "https://gitlab.com"),
        ("https://gitlab.example:8443/", "https://gitlab.example:8443"),
    ],
)
def test_normalize_origin_is_a_stable_instance_identity(value: str, expected: str) -> None:
    assert normalize_origin(value) == expected


@pytest.mark.parametrize("value", ["http://gitlab.com", "https://gitlab.com/group", "https://u:p@gitlab.com"])
def test_normalize_origin_refuses_an_ambiguous_issuer(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_origin(value)


def test_device_flow_uses_the_instance_endpoint_and_api_scope(monkeypatch) -> None:
    calls = []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return httpx.Response(200, json={"device_code": "device", "user_code": "code", "verification_uri": "https://gitlab.example/oauth/device", "expires_in": 300, "interval": 5})

    monkeypatch.setattr("engine.apps.web.gitlab_auth.httpx.AsyncClient", Client)
    result = asyncio.run(start_device_flow("gitlab.example", "client"))

    assert result.user_code == "code"
    assert calls == [("https://gitlab.example/oauth/authorize_device", {"data": {"client_id": "client", "scope": "api"}, "headers": {"Accept": "application/json"}})]


def test_device_flow_slow_down_increases_interval(monkeypatch) -> None:
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def post(self, *_args, **_kwargs): return httpx.Response(400, json={"error": "slow_down"})

    monkeypatch.setattr("engine.apps.web.gitlab_auth.httpx.AsyncClient", Client)
    assert asyncio.run(poll_device_flow("gitlab.com", "client", "device", 5)).next_interval == 10


def test_refresh_distinguishes_reconnect_required_from_transient_failure(monkeypatch) -> None:
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def post(self, *_args, **_kwargs): return httpx.Response(400, json={"error": "invalid_grant"})

    monkeypatch.setattr("engine.apps.web.gitlab_auth.httpx.AsyncClient", Client)
    with pytest.raises(GitLabRefreshTokenInvalidError):
        asyncio.run(refresh_access_token("gitlab.com", "client", "refresh"))


def test_device_flow_rejects_a_non_json_response(monkeypatch) -> None:
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): return None
        async def post(self, *_args, **_kwargs): return httpx.Response(502, content=b"bad gateway")

    monkeypatch.setattr("engine.apps.web.gitlab_auth.httpx.AsyncClient", Client)
    with pytest.raises(GitLabAuthError, match="invalid device authorization response"):
        asyncio.run(start_device_flow("gitlab.com", "client"))
