"""A deliberately narrow, authenticated Streamable HTTP surface."""

from dataclasses import dataclass
import secrets
from urllib.parse import urlsplit

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send


@dataclass(frozen=True)
class Settings:
    token: str
    repository: str
    workflow: str
    public_url: str
    engine_url: str = "http://127.0.0.1:8000"

    def __post_init__(self) -> None:
        if len(self.token) < 32 or any(c.isspace() for c in self.token):
            raise ValueError("MCP token must contain at least 32 non-whitespace characters")
        if not self.repository.strip() or not self.workflow.strip():
            raise ValueError("MCP repository and workflow are required")
        public = urlsplit(self.public_url)
        if (public.scheme != "https" or not public.hostname or public.username
                or public.password or public.path not in ("", "/")
                or public.query or public.fragment):
            raise ValueError("MCP public URL must be an HTTPS origin, without a path")
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
        if scope["type"] == "http":
            headers = Request(scope).headers.getlist("authorization")
            if len(headers) != 1 or not secrets.compare_digest(headers[0].encode(), self.expected):
                response = JSONResponse(
                    {"error": "Unauthorized"}, status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> ASGIApp:
    public = urlsplit(settings.public_url)
    mcp = FastMCP(
        "OpenEngine", stateless_http=True, json_response=True,
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

    return BearerAuth(mcp.streamable_http_app(), settings.token)
