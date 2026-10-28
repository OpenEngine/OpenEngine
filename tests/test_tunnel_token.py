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

    def build(*, token: str | None = TOKEN, github_login: bool = False, public_url: bool = True):
        for name in (
            "ENGINE_HOST", "ENGINE_PORT", "ENGINE_SQLITE_PATH", "ENGINE_GRAPH_STATE_DIRECTORY",
            "ENGINE_GITHUB_LOGIN_CLIENT_ID", "ENGINE_GITHUB_LOGIN_REDIRECT_URI",
            "ENGINE_GITHUB_LOGIN_CLIENT_SECRET", "ENGINE_SERVICE_TOKEN", "ENGINE_ONBOARDING_TOKEN",
        ):
            monkeypatch.delenv(name, raising=False)
        if github_login:
            monkeypatch.setenv("ENGINE_GITHUB_LOGIN_CLIENT_ID", "example-client")
            monkeypatch.setenv("ENGINE_GITHUB_LOGIN_CLIENT_SECRET", "example-secret")
            monkeypatch.setenv("ENGINE_GITHUB_LOGIN_REDIRECT_URI", f"https://{PUBLIC_HOST}/api/auth/github/callback")
        if token is not None:
            monkeypatch.setenv("ENGINE_SERVICE_TOKEN", token)
        monkeypatch.setenv("ENGINE_STATE_DIRECTORY", str(tmp_path / "state"))
        monkeypatch.chdir(tmp_path)
        config = tmp_path / "engine.toml"
        config.write_text((f'public_url = "https://{PUBLIC_HOST}"\n' if public_url else "")
                          + ("[access]\noperators = [42]\n" if github_login else ""))
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


def test_tunnel_token_also_works_with_github_login(tunnelled) -> None:
    assert ask(tunnelled(github_login=True)) == 401
    assert ask(tunnelled(github_login=True), headers={"Authorization": "Bearer wrong-" + TOKEN}) == 401
    assert ask(tunnelled(github_login=True), headers={"Authorization": f"Bearer {TOKEN}"}) == 200


def test_direct_local_cli_still_works_without_github_login(tunnelled) -> None:
    assert ask(tunnelled(public_url=False), host="127.0.0.1") == 200


def test_forwarding_cannot_bypass_auth_by_omitting_public_url(tunnelled) -> None:
    assert ask(tunnelled(public_url=False), host="127.0.0.1", headers={"X-Forwarded-For": "203.0.113.7"}) == 401


def test_public_browser_can_sign_in_with_token_and_sign_out(tunnelled) -> None:
    async def scenario():
        app = tunnelled()
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=f"https://{PUBLIC_HOST}"
        ) as client:
            status = (await client.get("/api/auth/github/status")).json()
            assert status["loginRequired"] and status["tokenLogin"]
            assert not status["authenticated"]
            assert (await client.get("/api/config")).status_code == 401
            assert (await client.post("/api/auth/token/login", data={"token": TOKEN},
                                      headers={"Origin": "https://attacker.example"})).status_code == 403
            wrong = await client.post("/api/auth/token/login", data={"token": "wrong"},
                                      headers={"Origin": f"https://{PUBLIC_HOST}"})
            assert "error=token" in wrong.headers["location"]
            signed_in = await client.post("/api/auth/token/login", data={"token": TOKEN},
                                          headers={"Origin": f"https://{PUBLIC_HOST}"})
            assert signed_in.status_code == 303
            assert "Secure" in signed_in.headers["set-cookie"]
            assert "HttpOnly" in signed_in.headers["set-cookie"]
            assert (await client.get("/api/auth/github/status")).json()["authenticated"]
            assert (await client.get("/api/config")).status_code == 200
            cookie = client.cookies.get("engine_token_session")
            await client.post("/api/auth/github/logout")
            assert (await client.get("/api/config")).status_code == 401
            assert (await client.get("/api/config", headers={"Cookie": f"engine_token_session={cookie}"})).status_code == 401

    asyncio.run(scenario())


def test_token_browser_session_ends_when_token_rotates(tunnelled, monkeypatch) -> None:
    async def scenario():
        app = tunnelled()
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=f"https://{PUBLIC_HOST}"
        ) as client:
            await client.post("/api/auth/token/login", data={"token": TOKEN},
                              headers={"Origin": f"https://{PUBLIC_HOST}"})
            monkeypatch.setenv("ENGINE_SERVICE_TOKEN", "r" * 40)
            assert (await client.get("/api/config")).status_code == 401

    asyncio.run(scenario())
