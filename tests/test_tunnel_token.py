"""A daemon reached through a tunnel answers its graph API only with the token.

Cloudflare Tunnel and `tailscale serve` run on the daemon's own computer and
forward to 127.0.0.1, so binding to loopback keeps nobody out: once
`public_url` names the tunnel's address, anyone who has that address reaches
the daemon, which runs agents and shell commands. Without GitHub login nothing
else stands in their way, so the service token must.
"""

import asyncio
import subprocess
from unittest.mock import AsyncMock
from pathlib import Path

import httpx
import pytest

from engine.apps.web.__main__ import build_app

PUBLIC_HOST = "openengine.example.com"
TOKEN = "t" * 40


@pytest.fixture
def tunnelled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Build the daemon as `engine-web` would, with `public_url` set to a tunnel's address and no GitHub login."""

    def build(*, token: str | None = TOKEN, github_login: bool = False, public_url: bool | str = True):
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
        address = public_url if isinstance(public_url, str) else f"https://{PUBLIC_HOST}"
        config.write_text((f'public_url = "{address}"\n' if public_url else "")
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
    assert ask(tunnelled(public_url=False, token=None), host="127.0.0.1") == 200


def test_a_local_token_protected_browser_is_offered_sign_in(tunnelled) -> None:
    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=tunnelled(public_url=False)),
            base_url="http://127.0.0.1",
        ) as client:
            status = (await client.get("/api/auth/github/status")).json()
            assert status["loginRequired"] and status["tokenLogin"]
            response = await client.post("/api/auth/token/login", data={"token": TOKEN},
                                         headers={"Origin": "http://127.0.0.1"})
            assert response.status_code == 303
            assert (await client.get("/api/config")).status_code == 200

    asyncio.run(scenario())


BROWSER_APIS = ("/api/runs", "/graph/api/graphs", "/api/v1/graphs")


def _local(app, scenario):
    """Run `scenario(client)` against `app` as a browser or CLI on the daemon's own computer."""

    async def run():
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            await scenario(client)

    asyncio.run(run())


def test_a_local_token_puts_the_login_wall_on_every_api(tunnelled) -> None:
    async def scenario(client):
        for path in BROWSER_APIS:
            assert (await client.get(path)).status_code == 401, path
        assert (await client.get("/api/health")).status_code == 200
        # The browser UI itself still loads, so it can show the token sign-in form.
        assert (await client.get("/")).status_code != 401

    _local(tunnelled(public_url=False), scenario)


def test_a_local_token_session_reaches_the_browser_apis(tunnelled) -> None:
    async def scenario(client):
        signed_in = await client.post("/api/auth/token/login", data={"token": TOKEN},
                                      headers={"Origin": "http://127.0.0.1"})
        assert signed_in.status_code == 303
        assert client.cookies.get("engine_token_session")
        for path in BROWSER_APIS:
            assert (await client.get(path)).status_code == 200, path
        assert (await client.get("/")).status_code != 401

    _local(tunnelled(public_url=False), scenario)


def test_a_local_token_bearer_reaches_the_cli_api(tunnelled) -> None:
    async def scenario(client):
        bearer = {"Authorization": f"Bearer {TOKEN}"}
        assert (await client.get("/api/v1/graphs", headers=bearer)).status_code == 200
        assert (await client.get("/", headers=bearer)).status_code != 401

    _local(tunnelled(public_url=False), scenario)


def test_a_plain_local_run_without_a_token_stays_open(tunnelled) -> None:
    async def scenario(client):
        for path in BROWSER_APIS:
            assert (await client.get(path)).status_code == 200, path

    _local(tunnelled(public_url=False, token=None), scenario)


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


def test_the_token_cannot_be_traded_for_a_session_that_marks_repositories_trusted(tunnelled, tmp_path) -> None:
    """The service token is barred from trust writes; signing in with it must not lift that bar.

    Setup: a tunnelled daemon (public_url, no GitHub login) with a registered,
    undecided repository. The caller holds only the service token and is a
    script, so it sets any Origin it likes.
    """

    async def scenario():
        tunnelled()  # Sets the daemon's environment; rebuilt below with a repository.
        subprocess.run(["git", "init", "-q", str(tmp_path / "checkout")], check=True)
        config = tmp_path / "engine.toml"
        config.write_text(f'public_url = "https://{PUBLIC_HOST}"\n[repos]\n"owner/repo" = "{tmp_path / "checkout"}"\n')
        app = build_app(config)
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=f"https://{PUBLIC_HOST}"
        ) as client:
            bearer = {"Authorization": f"Bearer {TOKEN}"}
            trust = {"repository": "owner/repo", "trusted": True}
            origin = {"Origin": f"https://{PUBLIC_HOST}"}
            # The token alone is refused, by design.
            assert (await client.post("/api/v1/repositories/trust", json=trust,
                                      headers={**bearer, **origin})).status_code == 401
            # Trade it for a browser session with a forged Origin, and try again.
            signed_in = await client.post("/api/auth/token/login", data={"token": TOKEN}, headers=origin)
            assert signed_in.status_code == 303
            response = await client.post("/api/v1/repositories/trust", json=trust, headers=origin)
            assert response.status_code == 401, response.text
            assert "trusted = true" not in config.read_text()

    asyncio.run(scenario())


def test_a_token_session_cannot_mark_a_repository_trusted_through_onboarding(tunnelled, tmp_path, monkeypatch) -> None:
    """The bar on trust writes must hold on every route that writes trust, onboarding included.

    Setup: a tunnelled daemon (public_url, no GitHub login) with a registered,
    undecided repository; onboarding's preflight and starter registration are
    stubbed. The caller holds only the service token, signs in with it, and
    asks onboarding to re-add the checkout with {"trusted": true}.
    Expected: refused, and engine.toml records no trust decision.
    """
    from engine.apps.web import onboarding

    monkeypatch.setattr(onboarding, "preflight", lambda: ([], "codex"))
    monkeypatch.setattr(onboarding, "register_starters", AsyncMock())

    async def scenario():
        tunnelled()  # Sets the daemon's environment; rebuilt below with a repository.
        checkout = tmp_path / "checkout"
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)
        subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", "git@github.com:owner/repo.git"], check=True)
        config = tmp_path / "engine.toml"
        config.write_text(f'public_url = "https://{PUBLIC_HOST}"\n[repos]\n"owner/repo" = "{checkout}"\n')
        app = build_app(config)
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=f"https://{PUBLIC_HOST}"
        ) as client:
            origin = {"Origin": f"https://{PUBLIC_HOST}"}
            signed_in = await client.post("/api/auth/token/login", data={"token": TOKEN}, headers=origin)
            assert signed_in.status_code == 303
            response = await client.post("/api/repositories/onboard",
                                         json={"repository": str(checkout), "trusted": True}, headers=origin)
            if response.status_code == 202:
                job = response.json()
                for _ in range(200):
                    job = (await client.get(f"/api/repositories/onboard/{job['id']}")).json()
                    if job["status"] in {"complete", "failed"}:
                        break
                    await asyncio.sleep(0.01)
                assert job["status"] == "complete", job["problems"]
            assert "trusted = true" not in config.read_text(), config.read_text()
            assert response.status_code in {401, 403}, (response.status_code, response.text)

    asyncio.run(scenario())


def test_signing_out_ends_a_token_sessions_open_stream(tunnelled, tmp_path, monkeypatch) -> None:
    """Signing out must end what the session already has open, not only what it opens next.

    Setup: a tunnelled daemon (public_url, no GitHub login); a browser signs in
    with the service token, creates a chat and opens its approval-events stream.
    It then signs out. Expected: the open stream ends within a few rechecks.
    """
    import anyio

    monkeypatch.setattr("engine.apps.web.github_login._STREAM_RECHECK", 0.01)

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # The daemon's working checkout.
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-q", "--allow-empty", "-m", "start"], check=True)

    async def scenario():
        app = tunnelled()
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=f"https://{PUBLIC_HOST}"
        ) as client:
            origin = {"Origin": f"https://{PUBLIC_HOST}"}
            assert (await client.post("/api/auth/token/login", data={"token": TOKEN},
                                      headers=origin)).status_code == 303
            cookie = client.cookies.get("engine_token_session")
            config = (await client.get("/api/config")).json()
            agent = config["agents"][0]["id"]
            runner = config["runners"][0] if isinstance(config["runners"][0], str) else config["runners"][0]["id"]
            created = await client.post("/api/threads", json={"agentId": agent, "runner": runner}, headers=origin)
            assert created.status_code == 201, created.text
            thread = created.json()["id"]

            # Open the stream as the browser's EventSource would, and keep it open.
            sent: list[dict] = []
            disconnect = anyio.Event()
            scope = {
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
                "scheme": "https", "server": (PUBLIC_HOST, 443), "client": ("203.0.113.7", 50000),
                "path": f"/api/threads/{thread}/approval-events", "raw_path": b"", "query_string": b"",
                "root_path": "",
                "headers": [(b"host", PUBLIC_HOST.encode()), (b"accept", b"text/event-stream"),
                            (b"cookie", f"engine_token_session={cookie}".encode())],
            }

            async def receive():
                await disconnect.wait()
                return {"type": "http.disconnect"}

            async def send(message):
                sent.append(message)

            async with anyio.create_task_group() as group:
                group.start_soon(app, scope, receive, send)
                with anyio.fail_after(5):
                    while not any(m.get("body", b"").startswith(b": connected") for m in sent):
                        await anyio.sleep(0.01)
                assert sent[0]["status"] == 200

                await client.post("/api/auth/github/logout", headers=origin)
                assert (await client.get("/api/config")).status_code == 401

                await anyio.sleep(1)  # 100 rechecks.
                ended = any(m["type"] == "http.response.body" and not m.get("more_body", False) for m in sent)
                disconnect.set()
                group.cancel_scope.cancel()
            assert ended, "the stream kept running after sign-out"

    asyncio.run(scenario())


@pytest.mark.parametrize("public_url", ["https://openengine.example.com/engine", "https://openengine.example.com:443/engine"])
def test_token_login_accepts_the_deployment_origin(tunnelled, public_url):
    app = tunnelled(public_url=public_url)
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"https://{PUBLIC_HOST}") as client:
            response = await client.post("/api/auth/token/login", data={"token": TOKEN},
                                         headers={"Origin": f"https://{PUBLIC_HOST}"})
            assert response.status_code == 303
            assert "engine_token_session" in response.cookies
    asyncio.run(scenario())
