"""The web application's public MCP OAuth authorization server."""

import json
from pathlib import Path
import secrets
from urllib.parse import urlsplit

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from engine.apps.web.github_login import GitHubLogin
from engine.apps.web.mcp_oauth_clients import MAX_DOCUMENT, fetch_cimd, validate_client
from engine.apps.web.mcp_oauth_storage import OAuthStore, SigningKeys

PREFIX = "/api/oauth"
WELL_KNOWN = "/.well-known/oauth-authorization-server/api/oauth"
HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"}


def response(data, status=200):
    return JSONResponse(data, status, headers=HEADERS)


async def body(request):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_DOCUMENT:
            raise ValueError("request too large")
    return bytes(data)


class OAuthServer:
    def __init__(self, public_url: str, resource: str, database: str | Path, login: GitHubLogin):
        origin = urlsplit(public_url)
        if (origin.scheme != "https" or not origin.hostname or origin.path not in ("", "/")
                or origin.query or origin.fragment or origin.username or origin.password):
            raise ValueError("MCP OAuth public_url must be an HTTPS origin without a path")
        self.issuer = public_url.rstrip("/") + PREFIX
        self.resource = resource or public_url.rstrip("/") + "/mcp"
        parsed = urlsplit(self.resource)
        if parsed.scheme != "https" or not parsed.hostname or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("mcp_resource_url must be an absolute HTTPS URL without a fragment")
        self.login = login
        self.store = OAuthStore(database)
        self.keys = SigningKeys(str(database) + ".oauth-keys.json")

    def routes(self):
        return [Route(PREFIX + "/metadata", self.metadata),
                Route(WELL_KNOWN, self.metadata),
                Route(PREFIX + "/jwks", self.jwks),
                Route(PREFIX + "/register", self.register, methods=["POST"])]

    async def metadata(self, request):
        return response({
            "issuer": self.issuer,
            "authorization_endpoint": self.issuer + "/authorize",
            "token_endpoint": self.issuer + "/token",
            "registration_endpoint": self.issuer + "/register",
            "jwks_uri": self.issuer + "/jwks",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": ["mcp"],
            "client_id_metadata_document_supported": True,
            "authorization_response_iss_parameter_supported": True,
        })

    async def jwks(self, request):
        return response(self.keys.jwks())

    async def register(self, request):
        try:
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                raise ValueError("expected application/json")
            document = validate_client(json.loads(await body(request)))
        except (ValueError, UnicodeError):
            return response({"error": "invalid_client_metadata"}, 400)
        client_id = secrets.token_urlsafe(32)
        with self.store.transaction() as db:
            db.execute("INSERT INTO oauth_clients VALUES (?, ?)", (client_id, json.dumps(document)))
        return response({**document, "client_id": client_id}, 201)

    async def client(self, client_id):
        if client_id.startswith("https://"):
            try:
                return await fetch_cimd(client_id)
            except (ValueError, OSError, TimeoutError, httpx.HTTPError) as exc:
                raise ValueError("invalid client metadata") from exc
        with self.store.transaction() as db:
            row = db.execute("SELECT metadata FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
        if row is None:
            raise ValueError("unknown client")
        return json.loads(row["metadata"])
