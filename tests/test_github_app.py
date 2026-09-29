"""App credentials never borrow the host's GitHub login."""

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from engine.adapters.source_control.github import GitHubSourceControl
from engine.adapters.source_control.github.transports import (
    GitHubAppTransport, GitHubCliTransport, GitHubTransportError, server_github_transport,
)
from engine.adapters.source_control.github.worktree import configure_worktree


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("ENGINE_GITHUB_APP_ID", raising=False)
    monkeypatch.delenv("ENGINE_GITHUB_APP_PRIVATE_KEY_PATH", raising=False)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (tmp_path / "key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    (tmp_path / ".env").write_text("ENGINE_GITHUB_APP_ID=123\nENGINE_GITHUB_APP_PRIVATE_KEY_PATH=key.pem\n")
    return GitHubAppTransport(tmp_path / ".env"), key


def mock_api(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(transport=httpx.MockTransport(handler), **kw))


def test_mint_cache_refresh_expiry_and_repository_installations(app, monkeypatch):
    transport, key = app
    calls = []
    minted = []
    rejected = False

    def api(request):
        nonlocal rejected
        path = request.url.path
        calls.append(path)
        auth = request.headers.get("authorization", "").removeprefix("Bearer ")
        if path.endswith("/installation") or path.endswith("/access_tokens"):
            claims = jwt.decode(auth, key.public_key(), algorithms=["RS256"])
            assert claims["iss"] == "123"
            assert claims["iat"] <= time.time() - 59
            assert claims["exp"] <= time.time() + 600
        if path.endswith("/installation"):
            return httpx.Response(200, json={"id": 22 if "/other/" in path else 11})
        if path.endswith("/access_tokens"):
            minted.append(path)
            return httpx.Response(201, json={"token": f"installation-{len(minted)}", "expires_at": datetime.fromtimestamp(time.time()+3600, timezone.utc).isoformat()})
        if not rejected:
            rejected = True
            return httpx.Response(401, json={"message": "expired"})
        return httpx.Response(200, json={"token_used": auth})

    mock_api(monkeypatch, api)

    async def exercise():
        assert await transport.installation_token("acme/api") == "installation-1"
        assert await transport.installation_token("acme/api") == "installation-1"
        assert await transport.request("GET", "/repos/acme/api/issues") == {"token_used": "installation-2"}
        assert len(minted) == 2
        assert await transport.installation_token("acme/second") == "installation-2"
        assert await transport.installation_token("other/api") == "installation-3"
        transport._tokens[11] = ("installation-2", time.time()+30)
        assert await transport.installation_token("acme/api") == "installation-4"

    asyncio.run(exercise())
    assert calls.count("/repos/acme/api/installation") == 1


def test_unauthorized_retries_only_once(app, monkeypatch):
    transport, _ = app
    transport.installation_token = AsyncMock(side_effect=["one", "two"])
    calls = []

    def api(request):
        calls.append(request)
        return httpx.Response(401, json={"message": "no"})

    mock_api(monkeypatch, api)
    with pytest.raises(GitHubTransportError, match="401"):
        asyncio.run(transport.request("GET", "/repos/acme/api/issues"))
    assert len(calls) == 2
    transport.installation_token.assert_awaited_with("acme/api", failed_token="one")


def test_app_login_and_bot_identity(app, monkeypatch):
    transport, _ = app
    paths = []

    def api(request):
        paths.append(request.url.path)
        if request.url.path == "/app":
            return httpx.Response(200, json={"slug": "openengine"})
        assert request.url.path == "/users/openengine[bot]"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"id": 42})

    mock_api(monkeypatch, api)
    source = GitHubSourceControl("", transport=transport)
    assert asyncio.run(source.authenticated_login("https://github.com/acme/api")) == "openengine[bot]"
    assert asyncio.run(transport.bot_identity()) == ("openengine[bot]", "42+openengine[bot]@users.noreply.github.com")
    assert "/user" not in paths


def test_configuration_reread_and_partial_fails_closed(app, monkeypatch):
    transport, _ = app
    assert isinstance(server_github_transport(transport.secret_file.with_name("engine.toml")), GitHubAppTransport)
    monkeypatch.setenv("ENGINE_GITHUB_APP_ID", "456")
    assert transport.configuration()[0] == "456"
    transport.secret_file.write_text("")
    with pytest.raises(GitHubTransportError, match="requires"):
        transport._jwt()
    monkeypatch.delenv("ENGINE_GITHUB_APP_ID")
    assert isinstance(server_github_transport(transport.secret_file.with_name("engine.toml")), GitHubCliTransport)


def test_worktree_configuration_is_local_and_contains_no_token(app):
    transport, _ = app
    transport.bot_identity = AsyncMock(return_value=("openengine[bot]", "42+openengine[bot]@users.noreply.github.com"))
    git = AsyncMock(return_value="git@github.com:acme/api.git")
    asyncio.run(configure_worktree(transport, "/worktree", git))
    configs = [call.args for call in git.await_args_list[1:]]
    assert all(args[:3] == ("/worktree", "config", "--worktree") for args in configs)
    assert any(args[-2:] == ("user.name", "openengine[bot]") for args in configs)
    assert any(args[-2:] == ("user.email", "42+openengine[bot]@users.noreply.github.com") for args in configs)
    assert any(args[-2:] == ("credential.helper", "") for args in configs)
    assert any(args[-2:] == ("url.https://github.com/.insteadOf", "git@github.com:") for args in configs)
    assert any("engine.adapters.source_control.github.credentials" in args[-1] for args in configs)
    assert not any("password=" in str(args) for args in configs)


def test_web_and_worker_choose_app_even_with_host_token(app):
    transport, _ = app
    from engine.apps.web.composition import Settings, build_capabilities
    from engine.apps.worker.composition import Settings as WorkerSettings
    from engine.apps.worker.composition import build_capabilities as build_worker

    for settings_type, build in [(Settings, build_capabilities), (WorkerSettings, build_worker)]:
        capabilities = build(settings_type(config_path=transport.secret_file.with_name("engine.toml"), github_token="host-token"))
        assert isinstance(capabilities.source_control._transport, GitHubAppTransport)
        assert capabilities.workspace_provider._configure_worktree is not None


def test_credential_helper_protocol(app, monkeypatch, capsys):
    import io
    from engine.adapters.source_control.github import credentials

    transport, _ = app
    monkeypatch.setattr(credentials.sys, "argv", ["helper", str(transport.secret_file), "get"])
    monkeypatch.setattr(credentials.sys, "stdin", io.StringIO("protocol=https\nhost=github.com\npath=acme/api.git\n\n"))
    mint = AsyncMock(return_value="installation-secret")
    monkeypatch.setattr(GitHubAppTransport, "installation_token", mint)
    credentials.main()
    mint.assert_awaited_once_with("acme/api")
    assert capsys.readouterr().out == "username=x-access-token\npassword=installation-secret\n\n"
    monkeypatch.setattr(credentials.sys, "stdin", io.StringIO("protocol=https\nhost=evil.example\npath=acme/api.git\n\n"))
    credentials.main()
    assert capsys.readouterr().out == "quit=true\n\n"
    assert mint.await_count == 1


def test_rotating_key_invalidates_cached_installations(app):
    transport, _ = app
    transport._jwt()
    transport._tokens[11] = ("old", time.time() + 3600)
    transport._installations["acme/api"] = 11
    replacement = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (transport.secret_file.parent / "key.pem").write_bytes(replacement.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    ))
    jwt.decode(transport._jwt(), replacement.public_key(), algorithms=["RS256"])
    assert not transport._tokens
    assert not transport._installations
