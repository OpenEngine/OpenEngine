"""MCP issuer tests use real SQLite migrations and cryptographic signatures."""

import asyncio
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
