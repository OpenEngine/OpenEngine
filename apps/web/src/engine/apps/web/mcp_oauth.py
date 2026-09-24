"""The web application's public MCP OAuth authorization server."""

import base64
import hashlib
import html
import json
import re
import time
from pathlib import Path
import secrets
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

from engine.apps.web.github_login import GitHubLogin, _return_to
from engine.apps.web.mcp_oauth_clients import MAX_DOCUMENT, fetch_cimd, validate_client
from engine.apps.web.mcp_oauth_storage import ACCESS_TOKEN_TTL, OAuthStore, SigningKeys

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
        _ = origin.port
        if (origin.scheme != "https" or not origin.hostname or origin.path not in ("", "/")
                or origin.query or origin.fragment or origin.username is not None or origin.password is not None
                or "\\" in public_url or any(c.isspace() for c in public_url)):
            raise ValueError("MCP OAuth public_url must be an HTTPS origin without a path")
        self.issuer = public_url.rstrip("/") + PREFIX
        self.resource = resource or public_url.rstrip("/") + "/mcp"
        parsed = urlsplit(self.resource)
        _ = parsed.port
        if (parsed.scheme != "https" or not parsed.hostname or parsed.fragment
                or parsed.username is not None or parsed.password is not None
                or "\\" in self.resource or any(c.isspace() for c in self.resource)):
            raise ValueError("mcp_resource_url must be an absolute HTTPS URL without a fragment")
        self.login = login
        self.store = OAuthStore(database)
        self.keys = SigningKeys(str(database) + ".oauth-keys.json")

    def routes(self):
        return [Route(PREFIX + "/metadata", self.metadata),
                Route(WELL_KNOWN, self.metadata),
                Route(PREFIX + "/jwks", self.jwks),
                Route(PREFIX + "/register", self.register, methods=["POST"]),
                Route(PREFIX + "/authorize", self.authorize, methods=["GET", "POST"]),
                Route(PREFIX + "/token", self.token, methods=["POST"])]

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
        return response(await run_in_threadpool(self.keys.jwks))

    async def register(self, request):
        try:
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                raise ValueError("expected application/json")
            document = validate_client(json.loads(await body(request)))
        except (ValueError, UnicodeError, RecursionError):
            return response({"error": "invalid_client_metadata"}, 400)
        client_id = secrets.token_urlsafe(32)
        if not await run_in_threadpool(self.store.register, client_id, document):
            return response({"error": "temporarily_unavailable"}, 503)
        return response({**document, "client_id": client_id}, 201)

    async def client(self, client_id):
        if client_id.startswith("https://"):
            try:
                return await fetch_cimd(client_id)
            except (ValueError, OSError, TimeoutError, httpx.HTTPError, RecursionError) as exc:
                raise ValueError("invalid client metadata") from exc
        row = await self.store.run(lambda db: db.execute(
            "SELECT metadata FROM oauth_clients WHERE client_id = ? AND expires > ?",
            (client_id, int(time.time()))).fetchone(), write=False)
        if row is None:
            raise ValueError("unknown client")
        return json.loads(row["metadata"])

    async def allowed(self, login):
        # Reuse exactly the login gate, including fail-closed error semantics.
        if self.login.authorize is None:
            return False
        try:
            return bool(await self.login.authorize(login))
        except Exception:
            return False

    async def authorize(self, request):
        user = self.login._read_session(request)
        if user is None:
            destination = _return_to(request.url.path + "?" + request.url.query)
            return RedirectResponse("/login?" + urlencode({"return_to": destination}), 302, headers=HEADERS)
        try:
            params = unique_params(request.query_params.multi_items())
            client = await self.client(params.get("client_id", ""))
            redirect = params.get("redirect_uri", "")
            if redirect not in client["redirect_uris"]:
                raise ValueError("redirect_uri mismatch")
        except ValueError:
            # Never send errors to a redirect URI we have not validated.
            return response({"error": "invalid_request"}, 400)
        if params.get("resource") != self.resource:
            return self.authorization_response(params, error="invalid_target")
        if (params.get("response_type") != "code" or params.get("code_challenge_method") != "S256"
                or not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.get("code_challenge", ""))
                or "authorization_code" not in client["grant_types"]):
            return self.authorization_response(params, error="invalid_request")
        if params.get("scope", "mcp") != "mcp":
            return self.authorization_response(params, error="invalid_scope")
        if not await self.allowed(str(user["login"])):
            return self.authorization_response(params, error="access_denied")
        # The signed form token binds the session, all authorization parameters,
        # and a short expiry. No authorization data is trusted from the form.
        binding = digest(request.cookies.get("engine_session", "") + request.url.query)
        if request.method == "GET":
            payload = f"{binding}.{int(time.time()) + 600}.{secrets.token_urlsafe(16)}"
            csrf = payload + "." + self.login._sign(payload)
            escape = html.escape
            document = (
                '<!doctype html><html><head><meta charset="utf-8"><title>Authorize MCP client</title></head><body>'
                f'<h1>Authorize {escape(client["client_name"])}</h1>'
                f'<p>Signed in as {escape(str(user["login"]))}.</p>'
                f'<p>Client ID: {escape(params["client_id"])}</p>'
                f'<p>Redirect URI: {escape(redirect)}</p>'
                f'<p>Allow this client to use MCP at {escape(self.resource)} with your repository access?</p>'
                '<p>Client names are supplied by the client. Verify the client ID and destination.</p>'
                f'<form method="post" action="{escape(request.url.path + "?" + request.url.query)}">'
                f'<input type="hidden" name="csrf" value="{escape(csrf)}">'
                '<button name="decision" value="approve">Approve</button> '
                '<button name="decision" value="deny">Deny</button></form></body></html>'
            )
            return HTMLResponse(document, headers={**HEADERS,
                "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
                "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff"})
        try:
            form = await form_params(request)
            token = form.get("csrf", "")
            bound, expires, nonce, signature = token.split(".")
            if (bound != binding or int(expires) <= time.time() or not secrets.compare_digest(
                    signature.encode(), self.login._sign(f"{bound}.{expires}.{nonce}").encode())):
                raise ValueError("invalid CSRF token")
        except (ValueError, UnicodeError):
            return response({"error": "invalid_request"}, 400)
        if form.get("decision") != "approve":
            return self.authorization_response(params, error="access_denied")
        code = secrets.token_urlsafe(32)
        grant = {"sub": str(user["id"]), "login": str(user["login"]), "client_id": params["client_id"],
                 "resource": self.resource, "scope": "mcp", "redirect_uri": redirect,
                 "challenge": params["code_challenge"], "refresh": "refresh_token" in client["grant_types"]}
        await self.store.run(lambda db: db.execute("INSERT INTO oauth_codes VALUES (?, ?, ?)",
                            (digest(code), json.dumps(grant), int(time.time()) + 60)).rowcount)
        return self.authorization_response(params, code=code)

    def authorization_response(self, params, **values):
        values["iss"] = self.issuer
        if "state" in params:
            values["state"] = params["state"]
        redirect = params["redirect_uri"]
        return RedirectResponse(redirect + ("&" if "?" in redirect else "?") + urlencode(values),
                                302, headers=HEADERS)

    async def token(self, request):
        try:
            params = await form_params(request)
        except (ValueError, UnicodeError):
            return response({"error": "invalid_request"}, 400)
        if request.headers.get("authorization") or "client_secret" in params:
            return response({"error": "invalid_client"}, 400)
        if params.get("resource", self.resource) != self.resource:
            return response({"error": "invalid_target"}, 400)
        grant_type = params.get("grant_type")
        if grant_type == "authorization_code":
            return await run_in_threadpool(self.exchange_code, params)
        if grant_type == "refresh_token":
            return await self.refresh(params)
        return response({"error": "unsupported_grant_type"}, 400)

    def exchange_code(self, params):
        with self.store.transaction() as db:
            token_hash = digest(params.get("code", ""))
            row = db.execute("SELECT * FROM oauth_codes WHERE token_hash = ?", (token_hash,)).fetchone()
            if row is None or row["expires"] <= time.time():
                return response({"error": "invalid_grant"}, 400)
            grant = json.loads(row["payload"])
            verifier = params.get("code_verifier", "")
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            if (not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier)
                    or not secrets.compare_digest(challenge, grant["challenge"])
                    or params.get("client_id") != grant["client_id"]
                    or params.get("redirect_uri") != grant["redirect_uri"]
                    or grant["resource"] != self.resource):
                return response({"error": "invalid_grant"}, 400)
            db.execute("DELETE FROM oauth_codes WHERE token_hash = ?", (token_hash,))
            return self.issue(db, grant, secrets.token_urlsafe(32), int(time.time()) + 30 * 86400)

    async def refresh(self, params):
        token_hash = digest(params.get("refresh_token", ""))
        row = await self.store.run(lambda db: db.execute(
            "SELECT * FROM oauth_refresh_tokens WHERE token_hash = ?", (token_hash,)).fetchone(), write=False)
        if row is None:
            return response({"error": "invalid_grant"}, 400)
        grant = json.loads(row["payload"])
        if params.get("client_id") != grant["client_id"] or grant["resource"] != self.resource:
            return response({"error": "invalid_grant"}, 400)
        if params.get("scope", grant["scope"]) != grant["scope"]:
            return response({"error": "invalid_scope"}, 400)
        allowed = await self.allowed(grant["login"])
        return await run_in_threadpool(self.rotate_refresh, token_hash, grant, allowed)

    def rotate_refresh(self, token_hash, grant, allowed):
        # Re-read inside the write transaction AFTER the network permission
        # check: concurrent refreshes cannot both rotate a token successfully.
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM oauth_refresh_tokens WHERE token_hash = ?", (token_hash,)).fetchone()
            if row is None:
                return response({"error": "invalid_grant"}, 400)
            if not allowed or row["used"] or row["revoked"] or row["expires"] <= time.time():
                db.execute("UPDATE oauth_refresh_tokens SET revoked = 1 WHERE family = ?", (row["family"],))
                return response({"error": "invalid_grant"}, 400)
            db.execute("UPDATE oauth_refresh_tokens SET used = 1 WHERE token_hash = ?", (token_hash,))
            return self.issue(db, grant, row["family"], row["expires"])

    def issue(self, db, grant, family, expires):
        now = int(time.time())
        claims = {key: grant[key] for key in ("sub", "login", "client_id", "scope")}
        claims.update(iss=self.issuer, aud=self.resource, iat=now, exp=now + ACCESS_TOKEN_TTL)
        result = {"access_token": self.keys.sign(claims), "token_type": "Bearer",
                  "expires_in": ACCESS_TOKEN_TTL, "scope": grant["scope"]}
        if grant["refresh"]:
            refresh = secrets.token_urlsafe(32)
            db.execute("INSERT INTO oauth_refresh_tokens (token_hash, family, payload, expires) VALUES (?, ?, ?, ?)",
                       (digest(refresh), family, json.dumps(grant), expires))
            result["refresh_token"] = refresh
        return response(result)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def unique_params(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate parameter")
        result[key] = value
    return result


async def form_params(request):
    if request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
        raise ValueError("expected form encoding")
    return unique_params(parse_qsl((await body(request)).decode(), keep_blank_values=True, max_num_fields=30))
