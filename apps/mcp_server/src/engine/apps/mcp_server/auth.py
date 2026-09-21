"""External OIDC access-token verification; this service never issues tokens."""

import asyncio
import logging
import time
from urllib.parse import urlsplit

import httpx
import jwt
from mcp.server.auth.provider import AccessToken

logger = logging.getLogger(__name__)
ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512")


def https_url(value: str) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return bool(parsed.scheme == "https" and parsed.hostname and not (
        parsed.username or parsed.password or parsed.query or parsed.fragment
    ))


class OIDCTokenVerifier:
    """Implements the SDK TokenVerifier protocol with a five-minute JWKS cache."""

    def __init__(self, issuer: str, audience: str, emails: tuple[str, ...], *,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.issuer = issuer
        self.audience = audience
        self.emails = {email.casefold() for email in emails}
        self.transport = transport
        self.keys: list[jwt.PyJWK] = []
        self.expires = 0.0
        self.lock = asyncio.Lock()

    async def _refresh(self) -> None:
        async with httpx.AsyncClient(transport=self.transport, timeout=10, trust_env=False) as client:
            response = await client.get(self.issuer.rstrip("/") + "/.well-known/openid-configuration")
            response.raise_for_status()
            metadata = response.json()
            if (not isinstance(metadata, dict) or metadata.get("issuer") != self.issuer
                    or not https_url(metadata.get("jwks_uri"))):
                raise ValueError("Invalid OIDC discovery metadata")
            response = await client.get(metadata["jwks_uri"])
            response.raise_for_status()
            jwks = response.json()
            if not isinstance(jwks, dict):
                raise ValueError("Invalid JWKS")
            keys = jwt.PyJWKSet.from_dict(jwks).keys
        self.keys = keys
        self.expires = time.monotonic() + 300

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in ALGORITHMS or not isinstance(header.get("kid"), str):
                return None
            async with self.lock:
                refreshed = time.monotonic() >= self.expires
                if refreshed:
                    await self._refresh()
                matches = [key for key in self.keys if key.key_id == header["kid"]]
                if not matches and not refreshed:
                    await self._refresh()
                    matches = [key for key in self.keys if key.key_id == header["kid"]]
            if len(matches) != 1:
                return None
            key = matches[0]
            if key.public_key_use not in (None, "sig") or key.algorithm_name != header["alg"]:
                return None
            claims = jwt.decode(
                token, key.key, algorithms=[key.algorithm_name],
                issuer=self.issuer, audience=self.audience,
                options={"require": ["iss", "sub", "aud", "exp"], "strict_aud": True},
            )
            email = claims.get("email")
            if (not isinstance(email, str) or email.casefold() not in self.emails
                    or ("email_verified" in claims and claims["email_verified"] is not True)):
                logger.warning("OIDC allowlist rejected subject %r", claims["sub"])
                return None
            scopes = claims.get("scope", "")
            if not isinstance(scopes, str):
                return None
            return AccessToken(
                token=token, client_id=claims.get("client_id", claims["sub"]),
                subject=claims["sub"], scopes=scopes.split(),
                expires_at=claims["exp"], resource=self.audience,
            )
        except (jwt.PyJWTError, httpx.HTTPError, ValueError, KeyError, TypeError, OverflowError):
            # Do not expose tokens, claims, or provider error responses.
            return None
