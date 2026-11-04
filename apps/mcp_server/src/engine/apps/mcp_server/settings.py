"""Gateway settings, the only module here that reads the environment."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import os
import re
from urllib.parse import urlsplit

from .auth import https_url


_ISSUER_DEPENDENT = ("OE_MCP_OIDC_AUDIENCE", "OE_MCP_ALLOWED_EMAILS", "OE_MCP_OIDC_REQUIRED_SCOPES")


def _reject_without_issuer(configured: Iterable[str]) -> None:
    for name in configured:
        raise ValueError(f"{name} requires OE_MCP_OIDC_ISSUER; unset it for static-token mode")


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
            _reject_without_issuer(
                name for name, configured in (
                    ("OE_MCP_OIDC_AUDIENCE", self.oidc_audience is not None),
                    ("OE_MCP_ALLOWED_EMAILS", bool(self.allowed_emails)),
                    ("OE_MCP_OIDC_REQUIRED_SCOPES", bool(self.oidc_required_scopes)),
                ) if configured
            )
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

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        """Build once at startup; a missing required variable is named in the error."""
        environment = os.environ if environ is None else environ
        required = {}
        for name in ("OE_MCP_REPOSITORY", "OE_MCP_WORKFLOW", "OE_MCP_PUBLIC_URL"):
            if name not in environment:
                raise ValueError(f"missing required environment variable {name}")
            required[name] = environment[name]
        # Presence, not value: even empty OIDC variables indicate configuration.
        if "OE_MCP_OIDC_ISSUER" not in environment:
            _reject_without_issuer(name for name in _ISSUER_DEPENDENT if name in environment)
        return cls(
            token=environment.get("OE_MCP_TOKEN", ""),
            repository=required["OE_MCP_REPOSITORY"],
            workflow=required["OE_MCP_WORKFLOW"],
            public_url=required["OE_MCP_PUBLIC_URL"],
            oidc_issuer=environment.get("OE_MCP_OIDC_ISSUER"),
            oidc_audience=environment.get("OE_MCP_OIDC_AUDIENCE"),
            allowed_emails=tuple(email.strip() for email in
                                 environment.get("OE_MCP_ALLOWED_EMAILS", "").split(",") if email.strip()),
            oidc_required_scopes=tuple(environment.get("OE_MCP_OIDC_REQUIRED_SCOPES", "").split()),
            engine_url=environment.get("OE_MCP_ENGINE_URL", "http://127.0.0.1:4364"),
            engine_token=environment.get("OE_MCP_ENGINE_TOKEN", ""),
        )
