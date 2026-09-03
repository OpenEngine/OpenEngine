"""GitHub OAuth device flow and OS keychain credential storage.

The device flow asks GitHub for a user code, directs the user to
github.com/login/device to enter it, and polls until GitHub confirms.
No redirect server or port binding needed -- appropriate for a local tool.

The token lands in the OS keychain via `keyring`, which picks the right
backend automatically (Keychain on macOS, Secret Service on Linux,
Windows Credential Manager on Windows).

Callers:
    `GitHubCredentialStore` -- get/set/delete the stored token
    `start_device_flow`     -- kick off the OAuth dance; returns the codes
    `poll_device_flow`      -- check whether the user completed it; returns
                              a result that carries the new poll interval
                              when GitHub asks the client to slow down
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import time

import httpx
import keyring
import keyring.backend
import keyring.backends.fail

#: The keyring service name and username used for every installation.  One
#: machine, one token -- this is a single-user local tool.
_KEYRING_SERVICE = "openengine"
_KEYRING_USERNAME = "github-token"
_KEYRING_CLIENT_ID_USERNAME = "github-client-id"

#: GitHub OAuth endpoints.
_GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
_GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"

#: The OAuth scopes needed to open pull requests and post comments.
_SCOPES = "repo offline_access"

#: Per spec, `slow_down` requires adding this many seconds to the interval.
_SLOW_DOWN_PENALTY_SECONDS = 5


@dataclass(frozen=True, slots=True)
class DeviceFlowState:
    """The codes and URL returned by the first step of the device flow."""

    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


@dataclass(frozen=True, slots=True)
class DeviceFlowPending:
    """The user has not yet completed authorisation.

    `next_interval` is the number of seconds the caller should wait before
    polling again. It equals the current interval unless GitHub returned
    `slow_down`, in which case it is the current interval plus five seconds as
    the spec requires. The caller is responsible for updating its own state.
    """

    next_interval: int


@dataclass(frozen=True, slots=True)
class DeviceFlowComplete:
    """Authorisation finished; GitHub returned an OAuth token set."""

    access_token: str
    refresh_token: str | None = None
    expires_in: int | None = None
    refresh_token_expires_in: int | None = None


@dataclass(frozen=True, slots=True)
class StoredCredentials:
    """An OAuth token set stored atomically in the OS keychain."""

    access_token: str
    refresh_token: str | None = None
    expires_at: float | None = None
    refresh_token_expires_at: float | None = None


DeviceFlowPollResult = DeviceFlowPending | DeviceFlowComplete


class GitHubAuthError(RuntimeError):
    """Something went wrong during the OAuth flow."""


class GitHubRefreshTokenInvalidError(GitHubAuthError):
    """GitHub confirmed that the refresh token cannot be used again."""


class GitHubCredentialStore:
    """Thin wrapper around `keyring` for the GitHub access token.

    Stateless -- every call reads from or writes to the OS keychain directly.

    `set` raises `GitHubAuthError` when keyring resolves to its in-memory
    fallback backend, which happens on headless servers with no OS keychain.
    Silently appearing to succeed on such systems would cause the token to
    vanish on restart, so failing loudly is the right behaviour.
    """

    def _check_backend(self) -> None:
        try:
            backend = keyring.get_keyring()
            priority = backend.priority
        except (keyring.errors.NoKeyringError, NotImplementedError):
            raise GitHubAuthError(
                "no secure keyring backend available on this system; "
                "the value cannot be stored safely"
            )
        if priority < 1:
            raise GitHubAuthError(
                "no secure keyring backend available on this system; "
                "the value cannot be stored safely"
            )

    def get(self) -> str | None:
        """Return the stored token, or None when nothing is saved."""
        credentials = self.get_credentials()
        return credentials.access_token if credentials is not None else None

    def get_credentials(self) -> StoredCredentials | None:
        """Return the stored OAuth token set.

        Earlier versions wrote the access token as a bare keychain value.  Keep
        those connections working until their owner reconnects and receives a
        refresh token.
        """
        try:
            value = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
        except keyring.errors.NoKeyringError:
            return None
        if not value:
            return None
        try:
            data = json.loads(value)
        except json.JSONDecodeError:
            return StoredCredentials(access_token=value)
        if not isinstance(data, dict) or not isinstance(data.get("access_token"), str):
            return StoredCredentials(access_token=value)
        return StoredCredentials(
            access_token=data["access_token"],
            refresh_token=_optional_string(data.get("refresh_token")),
            expires_at=_optional_number(data.get("expires_at")),
            refresh_token_expires_at=_optional_number(
                data.get("refresh_token_expires_at")
            ),
        )

    def set(self, token: str) -> None:
        self.set_credentials(StoredCredentials(access_token=token))

    def set_credentials(self, credentials: StoredCredentials) -> None:
        """Store the complete token set in one keychain write.

        GitHub rotates access and refresh tokens together, so separate writes
        could leave a process with a mismatched pair after an interruption.
        """
        self._check_backend()
        keyring.set_password(
            _KEYRING_SERVICE,
            _KEYRING_USERNAME,
            json.dumps(
                {
                    "access_token": credentials.access_token,
                    "refresh_token": credentials.refresh_token,
                    "expires_at": credentials.expires_at,
                    "refresh_token_expires_at": credentials.refresh_token_expires_at,
                },
                separators=(",", ":"),
            ),
        )

    def delete(self) -> None:
        try:
            keyring.delete_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
        except (keyring.errors.PasswordDeleteError, keyring.errors.NoKeyringError):
            pass

    def get_client_id(self) -> str | None:
        """Return the stored OAuth client ID, or None when nothing is saved."""
        try:
            return keyring.get_password(_KEYRING_SERVICE, _KEYRING_CLIENT_ID_USERNAME)
        except keyring.errors.NoKeyringError:
            return None

    def set_client_id(self, client_id: str) -> None:
        self._check_backend()
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_CLIENT_ID_USERNAME, client_id)

    def delete_client_id(self) -> None:
        try:
            keyring.delete_password(_KEYRING_SERVICE, _KEYRING_CLIENT_ID_USERNAME)
        except (keyring.errors.PasswordDeleteError, keyring.errors.NoKeyringError):
            pass


async def start_device_flow(client_id: str) -> DeviceFlowState:
    """Request a device code from GitHub and return the codes to show the user."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            _GITHUB_DEVICE_CODE_URL,
            data={"client_id": client_id, "scope": _SCOPES},
            headers={"Accept": "application/json"},
        )
    if response.is_error:
        raise GitHubAuthError(
            f"GitHub returned {response.status_code} for device code request"
        )
    body = response.json()
    if "error" in body:
        raise GitHubAuthError(f"GitHub device code error: {body['error']}")
    return DeviceFlowState(
        device_code=body["device_code"],
        user_code=body["user_code"],
        verification_uri=body["verification_uri"],
        expires_in=int(body.get("expires_in", 900)),
        interval=int(body.get("interval", 5)),
    )


async def poll_device_flow(
    client_id: str,
    device_code: str,
    current_interval: int,
) -> DeviceFlowPollResult:
    """Poll once for the device flow outcome.

    Returns `DeviceFlowComplete` when the user has authorised the app.

    Returns `DeviceFlowPending` when they have not yet done so.
    `DeviceFlowPending.next_interval` carries the number of seconds to wait
    before the next poll: it matches `current_interval` for
    `authorization_pending`, and `current_interval + 5` for `slow_down` as
    the spec requires.

    Raises `GitHubAuthError` for unrecoverable errors (expired, denied, etc.).
    """
    async with httpx.AsyncClient() as client:
        response = await client.post(
            _GITHUB_TOKEN_URL,
            data={
                "client_id": client_id,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers={"Accept": "application/json"},
        )
    if response.is_error:
        raise GitHubAuthError(
            f"GitHub returned {response.status_code} while polling device flow"
        )
    body = response.json()
    error = body.get("error")
    if error == "authorization_pending":
        return DeviceFlowPending(next_interval=current_interval)
    if error == "slow_down":
        return DeviceFlowPending(next_interval=current_interval + _SLOW_DOWN_PENALTY_SECONDS)
    if error:
        raise GitHubAuthError(f"GitHub device flow error: {error}")
    token = body.get("access_token", "")
    if not token:
        raise GitHubAuthError("GitHub returned no access_token")
    return DeviceFlowComplete(
        access_token=token,
        refresh_token=_optional_string(body.get("refresh_token")),
        expires_in=_optional_int(body.get("expires_in")),
        refresh_token_expires_in=_optional_int(body.get("refresh_token_expires_in")),
    )


async def refresh_access_token(client_id: str, refresh_token: str) -> StoredCredentials:
    """Exchange a Device Flow refresh token for GitHub's rotated token pair."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                _GITHUB_TOKEN_URL,
                data={
                    "client_id": client_id,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                headers={"Accept": "application/json"},
            )
    except httpx.HTTPError as error:
        raise GitHubAuthError(
            f"could not refresh GitHub access token: {error}"
        ) from error
    if response.is_error:
        raise GitHubAuthError(
            f"GitHub returned {response.status_code} while refreshing access token"
        )
    try:
        body = response.json()
    except ValueError as error:
        raise GitHubAuthError("GitHub returned an invalid refresh response") from error
    if body.get("error") == "bad_refresh_token":
        raise GitHubRefreshTokenInvalidError("GitHub rejected the refresh token")
    if body.get("error"):
        raise GitHubAuthError(f"GitHub token refresh error: {body['error']}")
    token = body.get("access_token")
    next_refresh_token = body.get("refresh_token")
    if not isinstance(token, str) or not token:
        raise GitHubAuthError("GitHub returned no access_token while refreshing")
    if not isinstance(next_refresh_token, str) or not next_refresh_token:
        raise GitHubAuthError("GitHub returned no refresh_token while refreshing")
    now = time.time()
    return StoredCredentials(
        access_token=token,
        refresh_token=next_refresh_token,
        expires_at=_expiry_at(now, body.get("expires_in")),
        refresh_token_expires_at=_expiry_at(
            now, body.get("refresh_token_expires_in")
        ),
    )


def credentials_from_device_flow(result: DeviceFlowComplete) -> StoredCredentials:
    """Convert Device Flow's relative expiry values to durable timestamps."""
    now = time.time()
    return StoredCredentials(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_at=_expiry_at(now, result.expires_in),
        refresh_token_expires_at=_expiry_at(now, result.refresh_token_expires_in),
    )


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


def _optional_number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _expiry_at(now: float, seconds: object) -> float | None:
    duration = _optional_int(seconds)
    return now + duration if duration is not None else None


__all__ = [
    "DeviceFlowComplete",
    "DeviceFlowPending",
    "DeviceFlowState",
    "GitHubAuthError",
    "GitHubCredentialStore",
    "GitHubRefreshTokenInvalidError",
    "StoredCredentials",
    "credentials_from_device_flow",
    "poll_device_flow",
    "refresh_access_token",
    "start_device_flow",
]
