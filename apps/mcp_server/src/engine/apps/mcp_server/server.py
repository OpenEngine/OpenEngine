"""A deliberately narrow, authenticated Streamable HTTP surface."""

from dataclasses import dataclass
import re
import secrets
from urllib.parse import urlsplit

import httpx
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .auth import OIDCTokenVerifier, https_url


@dataclass(frozen=True)
class Settings:
    token: str
    repository: str
    workflow: str
    public_url: str
    engine_url: str = "http://127.0.0.1:4364"

    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    allowed_emails: tuple[str, ...] = ()
    oidc_required_scopes: tuple[str, ...] = ()

    @property
    def resource_url(self) -> str:
        return self.public_url.rstrip("/") + "/mcp"

    def __post_init__(self) -> None:
        if self.oidc_issuer is None:
            for name, configured in (
                ("OE_MCP_OIDC_AUDIENCE", self.oidc_audience is not None),
                ("OE_MCP_ALLOWED_EMAILS", bool(self.allowed_emails)),
                ("OE_MCP_OIDC_REQUIRED_SCOPES", bool(self.oidc_required_scopes)),
            ):
                if configured:
                    raise ValueError(f"{name} requires OE_MCP_OIDC_ISSUER; unset it for static-token mode")
        if (not self.oidc_issuer or self.token) and (len(self.token) < 32 or any(c.isspace() for c in self.token)):
            raise ValueError("MCP token must contain at least 32 non-whitespace characters")
        if not self.repository.strip() or not self.workflow.strip():
            raise ValueError("MCP repository and workflow are required")
        public = urlsplit(self.public_url)
        if (public.scheme != "https" or not public.hostname or public.username
                or public.password or public.path not in ("", "/")
                or public.query or public.fragment):
            raise ValueError("MCP public URL must be an HTTPS origin, without a path")
        if self.oidc_issuer is not None:
            if not https_url(self.oidc_issuer):
                raise ValueError("OE_MCP_OIDC_ISSUER must be an HTTPS URL")
            if not self.allowed_emails or any(
                not re.fullmatch(r"[^@\s,]+@[^@\s,]+", email) for email in self.allowed_emails
            ):
                raise ValueError("OE_MCP_ALLOWED_EMAILS must contain a non-empty email allowlist")
            if self.oidc_audience is not None and self.oidc_audience != self.resource_url:
                raise ValueError("OE_MCP_OIDC_AUDIENCE must equal the public MCP resource URL")
        if any(not re.fullmatch(r'[\x21\x23-\x5b\x5d-\x7e]+', scope)
               for scope in self.oidc_required_scopes):
            raise ValueError("OE_MCP_OIDC_REQUIRED_SCOPES must contain valid OAuth scope names")
        upstream = urlsplit(self.engine_url)
        if (upstream.scheme != "http" or upstream.hostname not in ("127.0.0.1", "localhost", "::1")
                or upstream.username or upstream.password or upstream.path not in ("", "/")
                or upstream.query or upstream.fragment):
            raise ValueError("OE URL must be a loopback HTTP origin")


class BearerAuth:
    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"].rstrip("/") == "/mcp":
            headers = Request(scope).headers.getlist("authorization")
            if len(headers) != 1 or not secrets.compare_digest(headers[0].encode(), self.expected):
                response = JSONResponse(
                    {"error": "Unauthorized"}, status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class ScopeChallenge:
    """Add scope guidance omitted by the SDK's authentication challenge."""

    def __init__(self, app: ASGIApp, scopes: tuple[str, ...]) -> None:
        self.app, self.scopes = app, scopes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_challenge(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] in (401, 403):
                message["headers"] = [
                    (name, value + (', scope="' + " ".join(self.scopes) + '"').encode()
                     if name.lower() == b"www-authenticate" else value)
                    for name, value in message["headers"]
                ]
            await send(message)
        await self.app(scope, receive, send_challenge)


def create_app(settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None,
               oidc_transport: httpx.AsyncBaseTransport | None = None) -> ASGIApp:
    public = urlsplit(settings.public_url)
    mcp = FastMCP(
        "OpenEngine", stateless_http=True, json_response=True,
        token_verifier=OIDCTokenVerifier(
            settings.oidc_issuer, settings.oidc_audience or settings.resource_url,
            settings.allowed_emails, transport=oidc_transport,
        ) if settings.oidc_issuer else None,
        auth=AuthSettings(
            issuer_url=settings.oidc_issuer, resource_server_url=settings.resource_url,
            required_scopes=list(settings.oidc_required_scopes), validate_token_resource=True,
        ) if settings.oidc_issuer else None,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[public.netloc, "127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=[settings.public_url.rstrip("/")],
        ),
    )

    @mcp.tool(annotations=ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True,
    ))
    async def create_workorder(prompt: str, depends_on_run_id: str | None = None) -> dict[str, str]:
        """Create an OE work order in the configured repository.

        Optionally set depends_on_run_id to wait for that work order to succeed.
        Otherwise execution starts immediately. Returns the new run ID without
        waiting for completion. Each call creates new work.
        """
        if not prompt.strip() or len(prompt) > 100_000:
            raise ValueError("prompt must contain 1–100000 characters and not be blank")
        payload = {
            "prompt": prompt.strip(), "repository": settings.repository,
            "workflowId": settings.workflow,
        }
        if depends_on_run_id is not None:
            if not depends_on_run_id.strip():
                raise ValueError("depends_on_run_id must not be blank")
            payload["dependsOnRunId"] = depends_on_run_id.strip()
        # Never retry this POST: a lost response can still mean work was started.
        async with httpx.AsyncClient(
            base_url=settings.engine_url, transport=transport, timeout=60,
            trust_env=False,
        ) as client:
            try:
                response = await client.post("/api/runs", json=payload)
            except httpx.RequestError as error:
                raise RuntimeError(
                    "OE could not confirm creation. Check the OE work-order list before retrying."
                ) from error
        if response.status_code != 201:
            raise RuntimeError(
                f"OE rejected creation (HTTP {response.status_code}). Check OE configuration and logs."
            )
        try:
            run_id = response.json()["runId"]
            if not isinstance(run_id, str) or not run_id:
                raise ValueError("missing run ID")
        except (ValueError, KeyError, TypeError) as error:
            raise RuntimeError("OE returned an invalid result. Check the work-order list before retrying.") from error
        return {"run_id": run_id}

    app = mcp.streamable_http_app()
    if settings.oidc_issuer:
        return ScopeChallenge(app, settings.oidc_required_scopes) if settings.oidc_required_scopes else app
    return BearerAuth(app, settings.token)
