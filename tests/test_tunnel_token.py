"""A daemon reached through a tunnel answers its graph API only with the token.

Cloudflare Tunnel and `tailscale serve` run on the daemon's own computer and
forward to 127.0.0.1, so binding to loopback keeps nobody out: once
`public_url` names the tunnel's address, anyone who has that address reaches
the daemon, which runs agents and shell commands. Without GitHub login nothing
else stands in their way, so the service token must.
"""

import asyncio
from pathlib import Path

import httpx
import pytest

from engine.apps.web.__main__ import build_app

PUBLIC_HOST = "openengine.example.com"
TOKEN = "t" * 40


@pytest.fixture
def tunnelled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build the daemon as `engine-web` would, with `public_url` set to a tunnel's address and no GitHub login."""

    def build(*, token: str | None = TOKEN):
        for name in (
            "ENGINE_HOST", "ENGINE_PORT", "ENGINE_SQLITE_PATH", "ENGINE_GRAPH_STATE_DIRECTORY",
            "ENGINE_GITHUB_LOGIN_CLIENT_ID", "ENGINE_GITHUB_LOGIN_REDIRECT_URI",
            "ENGINE_GITHUB_LOGIN_CLIENT_SECRET", "ENGINE_SERVICE_TOKEN", "ENGINE_ONBOARDING_TOKEN",
        ):
            monkeypatch.delenv(name, raising=False)
        if token is not None:
            monkeypatch.setenv("ENGINE_SERVICE_TOKEN", token)
        monkeypatch.setenv("ENGINE_STATE_DIRECTORY", str(tmp_path / "state"))
        monkeypatch.chdir(tmp_path)
        config = tmp_path / "engine.toml"
        config.write_text(f'public_url = "https://{PUBLIC_HOST}"\n')
        return build_app(config)

    return build


def ask(app, *, host: str = PUBLIC_HOST, headers: dict[str, str] | None = None) -> int:
    """GET /api/v1/graphs as a request arriving with `host`, and the status it got."""

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=f"https://{host}"
        ) as client:
            async with app.router.lifespan_context(app):
                return (await client.get("/api/v1/graphs", headers=headers or {})).status_code

    return asyncio.run(scenario())


def test_the_tunnel_address_is_refused_without_the_token(tunnelled) -> None:
    assert ask(tunnelled()) == 401


@pytest.mark.parametrize("authorization", ["Bearer wrong-" + TOKEN, TOKEN, f"Basic {TOKEN}"])
def test_the_tunnel_address_is_refused_with_a_wrong_token(tunnelled, authorization: str) -> None:
    assert ask(tunnelled(), headers={"Authorization": authorization}) == 401


def test_the_tunnel_address_answers_with_the_token(tunnelled) -> None:
    assert ask(tunnelled(), headers={"Authorization": f"Bearer {TOKEN}"}) == 200


def test_a_tunnel_address_with_no_token_configured_answers_nobody(tunnelled) -> None:
    """No token to check is no reason to let everyone in."""
    app = tunnelled(token=None)
    assert ask(app) == 401
    assert ask(app, headers={"Authorization": "Bearer "}) == 401


@pytest.mark.parametrize("forwarded", [
    {"X-Forwarded-For": "203.0.113.7"},
    {"Cf-Connecting-Ip": "203.0.113.7"},
])
def test_a_tunnelled_request_is_refused_even_when_it_names_a_loopback_host(tunnelled, forwarded) -> None:
    """A tunnel may hand the daemon `Host: 127.0.0.1`; what it forwarded still came from outside."""
    assert ask(tunnelled(), host="127.0.0.1", headers=forwarded) == 401
    assert ask(tunnelled(), host="127.0.0.1", headers={**forwarded, "Authorization": f"Bearer {TOKEN}"}) == 200
