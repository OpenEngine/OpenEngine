"""GitLab OAuth device flow and per-instance keychain credentials.

GitLab.com and every self-managed instance are separate OAuth issuers.  The
normalised origin is consequently part of every keychain entry: a credential
issued by one instance must never be sent to another one.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from urllib.parse import urlsplit

import httpx
import keyring

from engine.apps.web.oauth_credentials import (
    OAuthCredentialError,
    OAuthCredentialStore,
    StoredCredentials,
    _optional_string,
    expiry_at,
    optional_int,
)

_KEYRING_SERVICE = "openengine"
_TOKEN_PREFIX = "gitlab-token:"
_CLIENT_ID_PREFIX = "gitlab-client-id:"
_SCOPES = "api"
_DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class GitLabAuthError(RuntimeError):
    """A GitLab OAuth request or secure credential operation failed."""


class GitLabRefreshTokenInvalidError(GitLabAuthError):
    """GitLab rejected a refresh token; the user must connect again."""


@dataclass(frozen=True, slots=True)
class DeviceFlowState:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


@dataclass(frozen=True, slots=True)
class DeviceFlowPending:
    next_interval: int


@dataclass(frozen=True, slots=True)
class DeviceFlowComplete:
    access_token: str
    refresh_token: str | None = None
    expires_in: int | None = None
    refresh_token_expires_in: int | None = None


DeviceFlowPollResult = DeviceFlowPending | DeviceFlowComplete


def normalize_origin(value: str) -> str:
    """Return an HTTPS GitLab instance identity suitable for credential keys.

    A bare host means HTTPS. Paths other than ``/``, credentials, query text,
    fragments, and non-HTTPS schemes are rejected rather than being silently
    normalised into a different OAuth issuer.
    """
    raw = value.strip()
    if not raw:
        raise ValueError("GitLab instance URL is required")
    parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("GitLab instance URL must use HTTPS and name a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("GitLab instance URL must not include credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("GitLab instance URL must not include a path, query, or fragment")
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("GitLab instance URL has an invalid port") from error
    return f"https://{host}" + (f":{port}" if port not in {None, 443} else "")


class GitLabCredentialStore(OAuthCredentialStore):
    """One GitLab instance's OAuth tokens and non-secret application ID."""

    def __init__(self, origin: str = "https://gitlab.com") -> None:
        self.origin = normalize_origin(origin)
        super().__init__(_KEYRING_SERVICE, f"{_TOKEN_PREFIX}{self.origin}")

    def _check_backend(self) -> None:
        try:
            super()._check_backend()
        except OAuthCredentialError as error:
            raise GitLabAuthError(str(error)) from error

    def get_client_id(self) -> str | None:
        try:
            return keyring.get_password(_KEYRING_SERVICE, f"{_CLIENT_ID_PREFIX}{self.origin}")
        except keyring.errors.NoKeyringError:
            return None

    def set_client_id(self, client_id: str) -> None:
        self._check_backend()
        keyring.set_password(_KEYRING_SERVICE, f"{_CLIENT_ID_PREFIX}{self.origin}", client_id)

    def delete_client_id(self) -> None:
        try:
            keyring.delete_password(_KEYRING_SERVICE, f"{_CLIENT_ID_PREFIX}{self.origin}")
        except (keyring.errors.PasswordDeleteError, keyring.errors.NoKeyringError):
            pass


async def start_device_flow(origin: str, client_id: str) -> DeviceFlowState:
    origin = normalize_origin(origin)
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{origin}/oauth/authorize_device",
                data={"client_id": client_id, "scope": _SCOPES},
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as error:
        raise GitLabAuthError(f"could not start GitLab device flow: {error}") from error
    body = _response_body(response, "device authorization")
    return DeviceFlowState(
        device_code=_required_string(body, "device_code"),
        user_code=_required_string(body, "user_code"),
        verification_uri=_required_string(body, "verification_uri"),
        expires_in=_positive_int(body.get("expires_in"), 300),
        interval=_positive_int(body.get("interval"), 5),
    )


async def poll_device_flow(origin: str, client_id: str, device_code: str, interval: int) -> DeviceFlowPollResult:
    body = await _token_request(origin, {"client_id": client_id, "device_code": device_code, "grant_type": _DEVICE_GRANT})
    error = body.get("error")
    if error == "authorization_pending":
        return DeviceFlowPending(interval)
    if error == "slow_down":
        return DeviceFlowPending(interval + 5)
    if error:
        raise GitLabAuthError(f"GitLab device flow error: {error}")
    return _complete(body)


async def refresh_access_token(origin: str, client_id: str, refresh_token: str) -> StoredCredentials:
    body = await _token_request(origin, {"client_id": client_id, "refresh_token": refresh_token, "grant_type": "refresh_token"})
    if body.get("error") in {"invalid_grant", "invalid_token"}:
        raise GitLabRefreshTokenInvalidError("GitLab refresh token expired or was revoked")
    if body.get("error"):
        raise GitLabAuthError(f"GitLab token refresh error: {body['error']}")
    result = _complete(body)
    now = time.time()
    return StoredCredentials(result.access_token, result.refresh_token or refresh_token, expiry_at(now, result.expires_in), expiry_at(now, result.refresh_token_expires_in))


async def _token_request(origin: str, data: dict[str, str]) -> dict:
    origin = normalize_origin(origin)
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(f"{origin}/oauth/token", data=data, headers={"Accept": "application/json"})
    except httpx.HTTPError as error:
        raise GitLabAuthError(f"could not reach GitLab token endpoint: {error}") from error
    return _response_body(response, "token request", allow_oauth_error=True)


def _response_body(response: httpx.Response, action: str, *, allow_oauth_error: bool = False) -> dict:
    try:
        body = response.json()
    except ValueError as error:
        raise GitLabAuthError(f"GitLab returned an invalid {action} response") from error
    if not isinstance(body, dict):
        raise GitLabAuthError(f"GitLab returned an invalid {action} response")
    if response.is_error and not (allow_oauth_error and isinstance(body.get("error"), str)):
        raise GitLabAuthError(f"GitLab returned {response.status_code} for {action}")
    return body


def _required_string(body: dict, name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value:
        raise GitLabAuthError(f"GitLab returned no {name}")
    return value


def _positive_int(value: object, default: int) -> int:
    parsed = optional_int(value)
    return parsed if parsed and parsed > 0 else default


def _complete(body: dict) -> DeviceFlowComplete:
    return DeviceFlowComplete(_required_string(body, "access_token"), _optional_string(body.get("refresh_token")), optional_int(body.get("expires_in")), optional_int(body.get("refresh_token_expires_in")))


def credentials_from_device_flow(result: DeviceFlowComplete) -> StoredCredentials:
    now = time.time()
    return StoredCredentials(result.access_token, result.refresh_token, expiry_at(now, result.expires_in), expiry_at(now, result.refresh_token_expires_in))
