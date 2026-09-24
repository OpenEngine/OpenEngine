"""MCP issuer tests use real SQLite migrations and cryptographic signatures."""

import asyncio
import base64
import hashlib
import re
from urllib.parse import parse_qs, urlsplit
import json
import socket
from unittest.mock import patch

import httpx
import jwt
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from migrations.migration import upgrade
from engine.apps.web.github_login import GitHubLogin, GitHubLoginConfig
from engine.apps.web.mcp_oauth import OAuthServer, WELL_KNOWN
from engine.apps.web.mcp_oauth_clients import fetch_cimd


@pytest.fixture
def issuer(tmp_path):
    database = tmp_path / "state.db"
    upgrade(f"sqlite:///{database}")
    async def allowed(login):
        return login == "alice"
    login = GitHubLogin(GitHubLoginConfig("id", "secret", "https://oe.test/api/auth/github/callback"),
                        authorize=allowed)
    return OAuthServer("https://oe.test", "", database, login)


def browser(issuer):
    return TestClient(Starlette(routes=issuer.routes()), base_url="https://oe.test")


def test_discovery_registration_and_persistent_rotatable_keys(issuer):
    client = browser(issuer)
    metadata = client.get(WELL_KNOWN).json()
    assert metadata == client.get("/api/oauth/metadata").json()
    assert metadata["issuer"] == "https://oe.test/api/oauth"
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["authorization_response_iss_parameter_supported"] is True
    assert metadata["client_id_metadata_document_supported"] is True
    registered = client.post("/api/oauth/register", json={"redirect_uris": ["https://client.test/callback"]})
    assert registered.status_code == 201
    assert registered.json()["token_endpoint_auth_method"] == "none"
    assert "client_secret" not in registered.json()
    assert asyncio.run(issuer.client(registered.json()["client_id"]))["redirect_uris"] == ["https://client.test/callback"]
    before = client.get("/api/oauth/jwks").json()
    restarted = OAuthServer("https://oe.test", "", issuer.store.path, issuer.login)
    assert restarted.keys.jwks() == before
    token = restarted.keys.sign({"sub": "42"})
    key = jwt.PyJWK.from_dict(before["keys"][0]).key
    assert jwt.decode(token, key, algorithms=["ES256"])["sub"] == "42"
    restarted.keys.rotate()
    assert len(issuer.keys.jwks()["keys"]) == 2
    assert jwt.get_unverified_header(issuer.keys.sign({"sub": "42"}))["kid"] != before["keys"][0]["kid"]
    assert issuer.keys.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("document", [None, {}, {"redirect_uris": ["https://a/#fragment"]},
    {"redirect_uris": ["http://remote.test/cb"]}, {"redirect_uris": ["https://a"], "token_endpoint_auth_method": "client_secret_basic"}])
def test_registration_rejects_invalid_metadata(issuer, document):
    assert browser(issuer).post("/api/oauth/register", content=json.dumps(document),
                                headers={"Content-Type": "application/json"}).status_code == 400


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "fe80::1", "192.168.1.2", "0.0.0.0"])
def test_cimd_rejects_nonpublic_addresses(address):
    async def run():
        loop = asyncio.get_running_loop()
        async def resolve(*args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]
        with patch.object(loop, "getaddrinfo", resolve):
            with pytest.raises(ValueError, match="not public"):
                await fetch_cimd("https://client.test/metadata.json")
    asyncio.run(run())


def test_cimd_pins_address_and_checks_document():
    async def run():
        loop = asyncio.get_running_loop()
        async def resolve(*args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        def serve(request):
            assert request.url.host == "8.8.8.8"
            assert request.headers["host"] == "client.test"
            assert request.extensions["sni_hostname"] == "client.test"
            return httpx.Response(200, json={"client_id": "https://client.test/metadata.json",
                                           "redirect_uris": ["https://client.test/cb"]})
        transport = httpx.AsyncClient(transport=httpx.MockTransport(serve))
        with patch.object(loop, "getaddrinfo", resolve), patch("engine.apps.web.mcp_oauth_clients.httpx.AsyncClient", return_value=transport):
            assert (await fetch_cimd("https://client.test/metadata.json"))["redirect_uris"] == ["https://client.test/cb"]
    asyncio.run(run())


def session_browser(issuer):
    client = TestClient(issuer.login.middleware(Starlette(routes=issuer.routes())), base_url="https://oe.test")
    client.cookies.set("engine_session", issuer.login._make_session_cookie(42, "alice"))
    return client


def authorization(client, **overrides):
    registered = client.post("/api/oauth/register", json={"client_name": "Test client", "redirect_uris": ["https://client.test/cb"]}).json()
    verifier = "v" * 43
    params = {"client_id": registered["client_id"], "redirect_uri": "https://client.test/cb", "response_type": "code",
              "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("="),
              "code_challenge_method": "S256", "resource": "https://oe.test/mcp", "state": "opaque-state"}
    params.update(overrides)
    return params, verifier


def consent(client, params, decision="approve"):
    page = client.get("/api/oauth/authorize", params=params, follow_redirects=False)
    assert page.status_code == 200, page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    return client.post("/api/oauth/authorize", params=params, data={"csrf": csrf, "decision": decision}, follow_redirects=False)


def code_grant(client, **overrides):
    params, verifier = authorization(client, **overrides)
    result = consent(client, params)
    query = parse_qs(urlsplit(result.headers["location"]).query)
    assert query["state"] == ["opaque-state"]
    assert query["iss"] == ["https://oe.test/api/oauth"]
    return {"grant_type": "authorization_code", "code": query["code"][0], "client_id": params["client_id"],
            "redirect_uri": params["redirect_uri"], "resource": params["resource"], "code_verifier": verifier}


def refresh(client, data, token):
    return client.post("/api/oauth/token", data={"grant_type": "refresh_token", "client_id": data["client_id"],
                                               "resource": "https://oe.test/mcp", "refresh_token": token})


def test_full_flow_and_rotation_reuse_revokes_family(issuer):
    client = session_browser(issuer)
    data = code_grant(client)
    first = client.post("/api/oauth/token", data=data)
    assert first.status_code == 200, first.text
    first = first.json()
    key = jwt.PyJWK.from_dict(issuer.keys.jwks()["keys"][0]).key
    claims = jwt.decode(first["access_token"], key, algorithms=["ES256"], audience="https://oe.test/mcp", issuer=issuer.issuer)
    assert claims["sub"] == "42" and claims["login"] == "alice"
    assert claims["client_id"] == data["client_id"] and claims["scope"] == "mcp"
    assert claims["exp"] - claims["iat"] == 900
    assert client.post("/api/oauth/token", data=data).json()["error"] == "invalid_grant"
    second = refresh(client, data, first["refresh_token"])
    assert second.status_code == 200
    second = second.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert refresh(client, data, first["refresh_token"]).status_code == 400
    assert refresh(client, data, second["refresh_token"]).status_code == 400
    with issuer.store.transaction() as db:
        rows = db.execute("SELECT * FROM oauth_refresh_tokens").fetchall()
        assert len(rows) == 2 and all(row["revoked"] for row in rows)
        assert all(first["refresh_token"] not in str(tuple(row)) for row in rows)


@pytest.mark.parametrize("field,value", [("code_verifier", "x" * 43), ("code_verifier", "short"),
    ("redirect_uri", "https://evil.test/cb"), ("client_id", "other"), ("resource", "https://other.test/mcp")])
def test_code_bindings(issuer, field, value):
    client = session_browser(issuer)
    data = code_grant(client)
    assert client.post("/api/oauth/token", data={**data, field: value}).status_code == 400
    assert client.post("/api/oauth/token", data=data).status_code == 200


@pytest.mark.parametrize("change,error", [({"resource": "https://other.test/mcp"}, "invalid_target"),
    ({"code_challenge_method": "plain"}, "invalid_request"), ({"code_challenge": ""}, "invalid_request"),
    ({"scope": "admin"}, "invalid_scope")])
def test_authorize_validation(issuer, change, error):
    client = session_browser(issuer)
    params, _ = authorization(client, **change)
    result = client.get("/api/oauth/authorize", params=params, follow_redirects=False)
    assert parse_qs(urlsplit(result.headers["location"]).query)["error"] == [error]


def test_untrusted_redirect_is_never_followed(issuer):
    client = session_browser(issuer)
    params, _ = authorization(client, redirect_uri="https://evil.test/cb")
    result = client.get("/api/oauth/authorize", params=params, follow_redirects=False)
    assert result.status_code == 400 and "location" not in result.headers


@pytest.mark.parametrize("error", [False, True])
def test_gate_revocation_and_errors_block_authorize_and_refresh(issuer, error):
    client = session_browser(issuer)
    data = code_grant(client)
    token = client.post("/api/oauth/token", data=data).json()["refresh_token"]
    async def refuse(login):
        if error:
            raise RuntimeError("provider unavailable")
        return False
    issuer.login.authorize = refuse
    params, _ = authorization(client)
    result = client.get("/api/oauth/authorize", params=params, follow_redirects=False)
    assert "error=access_denied" in result.headers["location"]
    assert refresh(client, data, token).status_code == 400
    async def restored(login):
        return True
    issuer.login.authorize = restored
    assert refresh(client, data, token).status_code == 400


def test_session_redirect_csrf_and_denial(issuer):
    anonymous = TestClient(issuer.login.middleware(Starlette(routes=issuer.routes())), base_url="https://oe.test")
    params, _ = authorization(anonymous)
    result = anonymous.get("/api/oauth/authorize", params=params, follow_redirects=False)
    assert result.headers["location"].startswith("/login?return_to=%2Fapi%2Foauth%2Fauthorize")
    assert anonymous.get(WELL_KNOWN).status_code == 200
    assert anonymous.get("/api/oauth/jwks").status_code == 200
    assert anonymous.post("/api/oauth/token", data={"grant_type": "unknown"}).status_code == 400
    client = session_browser(issuer)
    assert client.post("/api/oauth/authorize", params=params, data={"decision": "approve"}).status_code == 400
    page = client.get("/api/oauth/authorize", params=params)
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    changed = {**params, "state": "tampered"}
    assert client.post("/api/oauth/authorize", params=changed, data={"csrf": csrf, "decision": "approve"}).status_code == 400
    denied = consent(client, params, "deny")
    assert "error=access_denied" in denied.headers["location"]
    with issuer.store.transaction() as db:
        assert db.execute("SELECT count(*) FROM oauth_codes").fetchone()[0] == 0


def test_expired_code_refresh_and_admin_revocation(issuer):
    client = session_browser(issuer)
    data = code_grant(client)
    with issuer.store.transaction() as db:
        db.execute("UPDATE oauth_codes SET expires = 0")
    assert client.post("/api/oauth/token", data=data).status_code == 400
    data = code_grant(client)
    token = client.post("/api/oauth/token", data=data).json()["refresh_token"]
    with issuer.store.transaction() as db:
        db.execute("UPDATE oauth_refresh_tokens SET expires = 0")
    assert refresh(client, data, token).status_code == 400
    data = code_grant(client)
    token = client.post("/api/oauth/token", data=data).json()["refresh_token"]
    pending = code_grant(client)
    issuer.store.revoke_all()
    assert refresh(client, data, token).status_code == 400
    assert client.post("/api/oauth/token", data=pending).status_code == 400


def test_cimd_authorization_flow(issuer):
    client = session_browser(issuer)
    async def document(client_id):
        assert client_id == "https://client.test/metadata.json"
        return {"client_name": "CIMD client", "redirect_uris": ["https://client.test/cb"],
                "grant_types": ["authorization_code", "refresh_token"]}
    with patch("engine.apps.web.mcp_oauth.fetch_cimd", document):
        data = code_grant(client, client_id="https://client.test/metadata.json")
    result = client.post("/api/oauth/token", data=data)
    assert result.status_code == 200
    assert refresh(client, data, result.json()["refresh_token"]).status_code == 200


@pytest.mark.parametrize("status,document", [(302, {}), (200, {"client_id": "wrong"}), (200, {"padding": "a" * 17000})])
def test_cimd_rejects_redirects_mismatches_and_large_documents(status, document):
    async def run():
        loop = asyncio.get_running_loop()
        async def resolve(*args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        transport = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
            status, json=document, headers={"Location": "http://127.0.0.1/"})))
        with patch.object(loop, "getaddrinfo", resolve), patch("engine.apps.web.mcp_oauth_clients.httpx.AsyncClient", return_value=transport):
            with pytest.raises(ValueError):
                await fetch_cimd("https://client.test/metadata.json")
    asyncio.run(run())


def test_concurrent_refresh_detects_reuse(issuer):
    client = session_browser(issuer)
    data = code_grant(client)
    token = client.post("/api/oauth/token", data=data).json()["refresh_token"]
    async def run():
        ready = asyncio.Event()
        calls = 0
        async def allowed(login):
            nonlocal calls
            calls += 1
            if calls == 2:
                ready.set()
            await ready.wait()
            return True
        issuer.login.authorize = allowed
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=Starlette(routes=issuer.routes())), base_url="https://oe.test") as remote:
            form = {"grant_type": "refresh_token", "client_id": data["client_id"], "refresh_token": token}
            results = await asyncio.gather(*(remote.post("/api/oauth/token", data=form) for _ in range(2)))
            assert sorted(result.status_code for result in results) == [200, 400]
            replacement = next(result.json()["refresh_token"] for result in results if result.status_code == 200)
            form["refresh_token"] = replacement
            assert (await remote.post("/api/oauth/token", data=form)).status_code == 400
    asyncio.run(run())


def test_consent_rechecks_gate_and_binds_browser(issuer):
    client = session_browser(issuer)
    params, _ = authorization(client)
    page = client.get("/api/oauth/authorize", params=params)
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    other = session_browser(issuer)
    other.cookies.set("engine_session", issuer.login._make_session_cookie(43, "alice"))
    assert other.post("/api/oauth/authorize", params=params, data={"csrf": csrf, "decision": "approve"}).status_code == 400
    async def denied(login):
        return False
    issuer.login.authorize = denied
    result = client.post("/api/oauth/authorize", params=params, data={"csrf": csrf, "decision": "approve"}, follow_redirects=False)
    assert "error=access_denied" in result.headers["location"]


def test_refresh_survives_app_restart(issuer):
    client = session_browser(issuer)
    data = code_grant(client)
    original = client.post("/api/oauth/token", data=data).json()
    login = GitHubLogin(issuer.login.config, authorize=issuer.login.authorize)
    restarted = OAuthServer("https://oe.test", "", issuer.store.path, login)
    result = refresh(browser(restarted), data, original["refresh_token"])
    assert result.status_code == 200
    assert jwt.get_unverified_header(result.json()["access_token"])["kid"] == jwt.get_unverified_header(original["access_token"])["kid"]


@pytest.mark.parametrize("params", ["grant_type=refresh_token&grant_type=authorization_code", "client_id=a&client_id=b"])
def test_duplicate_token_parameters_are_rejected(issuer, params):
    result = browser(issuer).post("/api/oauth/token", content=params,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert result.status_code == 400
    assert result.json()["error"] == "invalid_request"


def test_cimd_timeout_fails_closed(issuer):
    client = session_browser(issuer)
    async def timeout(client_id):
        raise TimeoutError()
    params, _ = authorization(client, client_id="https://client.test/metadata.json")
    with patch("engine.apps.web.mcp_oauth.fetch_cimd", timeout):
        result = client.get("/api/oauth/authorize", params=params, follow_redirects=False)
    assert result.status_code == 400
    assert "location" not in result.headers


@pytest.mark.parametrize("public_url,resource", [("http://oe.test", ""), ("https://oe.test/path", ""),
    ("https://oe.test", "http://mcp.test"), ("https://oe.test", "https://mcp.test/#fragment")])
def test_invalid_issuer_resource_configuration_is_rejected(issuer, public_url, resource):
    with pytest.raises(ValueError):
        OAuthServer(public_url, resource, issuer.store.path, issuer.login)


def test_key_retirement_deadlines_and_compromise(issuer):
    from engine.apps.web.mcp_oauth_storage import SigningKeys
    keys = issuer.keys
    original = keys.jwks()["keys"][0]["kid"]
    with patch("engine.apps.web.mcp_oauth_storage.time.time", return_value=1000):
        keys.rotate()
    second = jwt.get_unverified_header(keys.sign({"sub": "42"}))["kid"]
    with patch("engine.apps.web.mcp_oauth_storage.time.time", return_value=1100):
        keys.rotate()
        assert len(keys.jwks()["keys"]) == 3
    with patch("engine.apps.web.mcp_oauth_storage.time.time", return_value=1900):
        restarted = SigningKeys(keys.path)
        assert original not in {key["kid"] for key in restarted.jwks()["keys"]}
        assert second in {key["kid"] for key in restarted.jwks()["keys"]}
        restarted.retire(second)
        assert len(keys.jwks()["keys"]) == 1
        active = keys.jwks()["keys"][0]["kid"]
        restarted.retire(active)
        assert active not in {key["kid"] for key in keys.jwks()["keys"]}
        token = keys.sign({"sub": "42"})
        assert jwt.decode(token, jwt.PyJWK.from_dict(keys.jwks()["keys"][0]).key,
                          algorithms=["ES256"])["sub"] == "42"
        with pytest.raises(ValueError, match="unknown"):
            restarted.retire("missing")


def test_legacy_key_ring_gets_fixed_retirement_deadline(issuer):
    from engine.apps.web.mcp_oauth_storage import SigningKeys
    issuer.keys.rotate()
    ring = issuer.keys._read()
    del ring["retire_at"]
    issuer.keys._save(ring)
    with patch("engine.apps.web.mcp_oauth_storage.time.time", return_value=1000):
        assert len(SigningKeys(issuer.keys.path).jwks()["keys"]) == 2
    with patch("engine.apps.web.mcp_oauth_storage.time.time", return_value=1900):
        assert len(SigningKeys(issuer.keys.path).jwks()["keys"]) == 1


def test_registration_cap_is_atomic_and_expired_rows_are_reclaimed(issuer):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=Starlette(routes=issuer.routes())),
                                    base_url="https://oe.test") as client:
            document = {"redirect_uris": ["https://client.test/cb"]}
            with patch("engine.apps.web.mcp_oauth_storage.MAX_CLIENTS", 2):
                results = await asyncio.gather(*(client.post("/api/oauth/register", json=document) for _ in range(8)))
                assert sorted(result.status_code for result in results) == [201, 201] + [503] * 6
                old_id = next(result.json()["client_id"] for result in results if result.status_code == 201)
                await issuer.store.run(lambda db: db.execute("UPDATE oauth_clients SET expires = 0").rowcount)
                with pytest.raises(ValueError, match="unknown client"):
                    await issuer.client(old_id)
                assert (await client.post("/api/oauth/register", json=document)).status_code == 201
                count = await issuer.store.run(lambda db: db.execute("SELECT count(*) FROM oauth_clients").fetchone()[0], write=False)
                assert count == 1
    asyncio.run(run())


def test_database_lock_does_not_block_event_loop_or_client_reads(issuer):
    import sqlite3
    import threading
    import time
    client_id = browser(issuer).post("/api/oauth/register", json={"redirect_uris": ["https://client.test/cb"]}).json()["client_id"]
    async def run():
        locked = threading.Event()
        release = threading.Event()
        def hold_lock():
            with sqlite3.connect(issuer.store.path) as db:
                db.execute("BEGIN IMMEDIATE")
                locked.set()
                release.wait(3)
        thread = threading.Thread(target=hold_lock)
        thread.start()
        await asyncio.to_thread(locked.wait)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=Starlette(routes=issuer.routes())),
                                        base_url="https://oe.test") as client:
                start = time.monotonic()
                pending = asyncio.create_task(client.post("/api/oauth/register", json={"redirect_uris": ["https://client.test/cb"]}))
                await asyncio.sleep(0.05)
                assert (await client.get(WELL_KNOWN)).status_code == 200
                assert await issuer.client(client_id)
                assert time.monotonic() - start < 1
                assert not pending.done()
                release.set()
                assert (await pending).status_code == 201
        finally:
            release.set()
            await asyncio.to_thread(thread.join)
    asyncio.run(run())


def test_registration_expiry_migrates_existing_clients(tmp_path):
    import sqlite3
    import time
    database = tmp_path / "upgrade.db"
    upgrade(f"sqlite:///{database}", "1099c8b7900d")
    with sqlite3.connect(database) as db:
        db.execute("INSERT INTO oauth_clients VALUES ('existing', '{}')")
    before = int(time.time())
    upgrade(f"sqlite:///{database}")
    with sqlite3.connect(database) as db:
        expires = db.execute("SELECT expires FROM oauth_clients WHERE client_id = 'existing'").fetchone()[0]
    assert before + 30 * 86400 <= expires <= int(time.time()) + 30 * 86400


def test_retire_key_admin_command(issuer):
    from engine.apps.web.mcp_oauth_storage import main
    kid = issuer.keys.jwks()["keys"][0]["kid"]
    with patch("sys.argv", ["mcp_oauth_storage", "retire-key", issuer.store.path, "--kid=" + kid]):
        main()
    assert kid not in {key["kid"] for key in issuer.keys.jwks()["keys"]}
