"""A deliberately narrow, authenticated Streamable HTTP surface."""

from collections import deque
from dataclasses import dataclass, field
import re
import secrets
from urllib.parse import quote, urlsplit

import httpx
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
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
    # Presented to OE on `POST /api/runs` when OE requires GitHub login. Separate
    # from `token`: a client's credential for the gateway never reaches OE.
    engine_token: str = field(default="", repr=False)

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
        if self.engine_token:
            if len(self.engine_token) < 32 or any(c.isspace() for c in self.engine_token):
                raise ValueError("OE_MCP_ENGINE_TOKEN must contain at least 32 non-whitespace characters")
            if self.engine_token == self.token:
                raise ValueError("OE_MCP_ENGINE_TOKEN must differ from OE_MCP_TOKEN")
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


def engine_login_problem(settings: Settings, *, transport: httpx.BaseTransport | None = None) -> str | None:
    """Why this gateway could not create work orders in OE, if that is knowable now.

    OE with GitHub login enabled rejects `POST /api/runs` without a service
    token; catching that at startup beats a 401 on the first tool call. An
    unreachable OE is not a problem here: the gateway may start first.
    """
    if settings.engine_token:
        return None
    try:
        with httpx.Client(base_url=settings.engine_url, transport=transport, timeout=5, trust_env=False) as client:
            status = client.get("/api/auth/github/status").json()
    except (httpx.HTTPError, ValueError):
        return None
    if isinstance(status, dict) and status.get("loginRequired") is True:
        return "OE requires GitHub login; set OE_MCP_ENGINE_TOKEN to OE's ENGINE_SERVICE_TOKEN"
    return None


def create_app(settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None,
               oidc_transport: httpx.AsyncBaseTransport | None = None) -> ASGIApp:
    public = urlsplit(settings.public_url)
    mcp = MCPServer(
        "OpenEngine",
        token_verifier=OIDCTokenVerifier(
            settings.oidc_issuer, settings.oidc_audience or settings.resource_url,
            settings.allowed_emails, transport=oidc_transport,
        ) if settings.oidc_issuer else None,
        auth=AuthSettings(
            issuer_url=settings.oidc_issuer, resource_server_url=settings.resource_url,
            required_scopes=list(settings.oidc_required_scopes), validate_token_resource=True,
        ) if settings.oidc_issuer else None,
    )

    @mcp.tool(annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True,
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
            headers = {"Authorization": f"Bearer {settings.engine_token}"} if settings.engine_token else {}
            try:
                response = await client.post("/api/runs", json=payload, headers=headers)
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

    async def request_engine(
        method: str, path: str, payload: dict | None = None,
        retry_hint: str = "Check workorder_status before retrying.",
    ) -> dict:
        headers = {"Authorization": f"Bearer {settings.engine_token}"} if settings.engine_token else {}
        async with httpx.AsyncClient(
            base_url=settings.engine_url, transport=transport, timeout=60, trust_env=False,
        ) as client:
            try:
                response = await client.request(method, path, json=payload, headers=headers)
            except httpx.RequestError as error:
                raise RuntimeError(f"OE could not confirm the request. {retry_hint}") from error
        if not response.is_success:
            raise RuntimeError(f"OE rejected the request (HTTP {response.status_code}).")
        try:
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("expected an object")
        except ValueError as error:
            raise RuntimeError("OE returned an invalid result. Check OE before retrying.") from error
        return result

    def identifier(value: str) -> str:
        if not value.strip() or value.strip() in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError("identifier must be non-blank and contain no path separators")
        return quote(value.strip(), safe="")

    async def workorder(run_id: str) -> tuple[str, dict]:
        key = identifier(run_id)
        run = await request_engine("GET", f"/api/runs/{key}")
        if run.get("repository") != settings.repository:
            raise ValueError("work order is outside the configured repository")
        return key, run

    async def graph_state(key: str, run: dict) -> tuple[dict, dict]:
        if run.get("phase") == "scheduled":
            snapshot = {"status": "scheduled", "activeExecutions": [], "nextNodes": [], "values": {}}
            graph_id = run["workflowId"]
        else:
            snapshot = await request_engine("GET", f"/graph/api/runs/{key}")
            graph_id = snapshot["graphId"]
        topology = await request_engine("GET", f"/graph/api/graphs/{identifier(graph_id)}")
        return snapshot, topology

    async def transcript(key: str, node: str, last_n: int) -> list[dict]:
        events = await request_engine("GET", f"/api/runs/{key}/graph-events")
        return [
            event["payload"] for event in events["events"]
            if event.get("nodeId") == node and event.get("type") == "transcript"
        ][-last_n:]

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def workorder_status(run_id: str) -> dict[str, object]:
        """Return status, current nodes, topology, recent transcript and PR URL.

        Each current node includes its last five transcript messages.
        active_executions lists execution_id, node_id and separate messages for
        each parallel task; an idle frontier uses nextNodes.
        """
        key, run = await workorder(run_id)
        snapshot, topology = await graph_state(key, run)
        nodes = list(dict.fromkeys(
            item["nodeId"] for item in snapshot["activeExecutions"]
        )) or snapshot["nextNodes"]
        # Download and partition the feed once, keeping only bounded snippets.
        by_node = {node: deque(maxlen=5) for node in nodes}
        by_execution = {
            item["executionId"]: deque(maxlen=5) for item in snapshot["activeExecutions"]
        }
        if nodes:
            events = await request_engine("GET", f"/api/runs/{key}/graph-events")
            for event in events["events"]:
                if event.get("type") == "transcript":
                    if event.get("nodeId") in by_node:
                        by_node[event["nodeId"]].append(event["payload"])
                    if event.get("executionId") in by_execution:
                        by_execution[event["executionId"]].append(event["payload"])
        values = snapshot.get("values", {})
        pr_url = values.get("pr_url") or next((
            value["pr_url"] for value in values.values()
            if isinstance(value, dict) and value.get("pr_url")
        ), None)
        return {
            "run_id": run["runId"], "status": snapshot["status"],
            "current_nodes": nodes, "topology": topology, "pr_url": pr_url,
            "transcript": {node: list(by_node[node]) for node in nodes},
            "active_executions": [
                {"node_id": item["nodeId"], "execution_id": item["executionId"],
                 "messages": list(by_execution[item["executionId"]])}
                for item in snapshot["activeExecutions"]
            ],
        }

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def node_status(run_id: str, nodename: str, last_n: int = 10) -> dict[str, object]:
        """Return the last n transcript messages from the selected node, oldest first.

        Use a nodeId from workorder_status's topology. last_n must be 1–1000.
        """
        identifier(nodename)
        if not 1 <= last_n <= 1000:
            raise ValueError("last_n must be between 1 and 1000")
        key, run = await workorder(run_id)
        snapshot, topology = await graph_state(key, run)
        node = nodename.strip()
        if not any(item["nodeId"] == node for item in topology["nodes"]):
            raise ValueError("unknown node")
        return {
            "run_id": run["runId"], "node": node, "status": snapshot["status"],
            "messages": await transcript(key, node, last_n),
        }

    @mcp.tool(annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True,
    ))
    async def steer_workorder(
        run_id: str, instruction: str, nodename: str | None = None,
        execution_id: str | None = None,
    ) -> dict[str, object]:
        """Send an instruction to an active node without resetting it.

        Select either a nodeId or an execution_id from workorder_status.
        Use execution_id when multiple tasks run the same node. To revisit an
        earlier node, use node_steer with the instruction in the same call.
        """
        if not instruction.strip() or len(instruction) > 100_000:
            raise ValueError("instruction must contain 1–100000 characters and not be blank")
        if nodename is not None and execution_id is not None:
            raise ValueError("give at most one of nodename or execution_id")
        payload = {"message": instruction.strip()}
        if execution_id is not None:
            identifier(execution_id)
            payload["execution"] = execution_id.strip()
        if nodename is not None:
            identifier(nodename)
            payload["node"] = nodename.strip()
        key, _ = await workorder(run_id)
        return await request_engine("POST", f"/graph/api/runs/{key}/steering", payload)

    @mcp.tool(annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True,
    ))
    async def node_steer(run_id: str, nodename: str, instruction: str) -> dict[str, object]:
        """Stop current execution and reset to the latest checkpoint before this node.

        Queue the instruction before restarting execution from that point.
        Earlier attempts remain in the transcript. The node must have been
        reached previously.
        """
        identifier(nodename)
        if not instruction.strip() or len(instruction) > 100_000:
            raise ValueError("instruction must contain 1–100000 characters and not be blank")
        key, _ = await workorder(run_id)
        return await request_engine(
            "POST", f"/graph/api/runs/{key}/transitions", {"node": nodename.strip(), "message": instruction.strip()},
        )

    def loop_summary(loop: dict) -> dict[str, object]:
        if loop.get("repository") != settings.repository:
            raise ValueError("loop is outside the configured repository")
        return {
            "loop_id": loop["loopId"], "name": loop["name"], "prompt": loop["prompt"],
            "every_minutes": loop["everyMinutes"], "active_hours": loop["activeHours"],
            "max_workorders": loop["maxWorkOrders"], "max_daily_spend": loop["maxDailySpend"],
            "running": loop["running"], "next_run_at": loop["nextRunAt"],
            "deferred_until": loop["deferredUntil"], "spent_today": loop["spentToday"],
            "workorders": [
                {"run_id": one["runId"], "name": one["name"], "phase": one["phase"]}
                for one in loop["workOrders"]
            ],
        }

    @mcp.tool(annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True,
    ))
    async def create_loop(
        name: str, prompt: str, every_minutes: int = 60, max_workorders: int | None = None,
        max_daily_spend: float | None = None, active_hours_start: str | None = None,
        active_hours_end: str | None = None,
    ) -> dict[str, object]:
        """Create a loop that prompts an agent every every_minutes in the configured repository.

        Each run may create, steer and resume work orders, one at a time.
        max_workorders per day, max_daily_spend in dollars and active hours
        (HH:MM, equal for any time) default to OE's loop settings. Each call
        creates a new loop; check loop_status before retrying.
        """
        if not name.strip() or len(name) > 200:
            raise ValueError("name must contain 1–200 characters and not be blank")
        if not prompt.strip() or len(prompt) > 100_000:
            raise ValueError("prompt must contain 1–100000 characters and not be blank")
        defaults = await request_engine("GET", "/api/loops/defaults")
        hours = defaults["activeHours"]
        payload = {
            "name": name.strip(), "prompt": prompt.strip(), "repository": settings.repository,
            "everyMinutes": every_minutes,
            "activeHours": {"start": active_hours_start or hours["start"],
                            "end": active_hours_end or hours["end"]},
            "maxWorkOrders": defaults["maxWorkOrders"] if max_workorders is None else max_workorders,
            "maxDailySpend": defaults["maxDailySpend"] if max_daily_spend is None else max_daily_spend,
        }
        # Never retry this POST: a lost response can still mean the loop was saved.
        return loop_summary(await request_engine(
            "POST", "/api/loops", payload, retry_hint="Check the OE loop list before retrying.",
        ))

    @mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
    async def loop_status(loop_id: str) -> dict[str, object]:
        """Return a loop's schedule, limits, spend today and the work orders it created.

        next_run_at is null while the loop is running; deferred_until names
        the work order the next run waits for. Use workorder_status for one.
        """
        return loop_summary(await request_engine("GET", f"/api/loops/{identifier(loop_id)}"))

    app = mcp.streamable_http_app(
        stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[public.netloc, "127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=[settings.public_url.rstrip("/")],
        ),
    )
    if settings.oidc_issuer:
        return ScopeChallenge(app, settings.oidc_required_scopes) if settings.oidc_required_scopes else app
    return BearerAuth(app, settings.token)
