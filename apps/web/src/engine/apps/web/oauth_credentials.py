"""Provider-neutral OAuth credentials and secure keychain storage."""

from __future__ import annotations

from dataclasses import dataclass
import json

import keyring


class OAuthCredentialError(RuntimeError):
    """Credentials could not be read or safely stored."""


@dataclass(frozen=True, slots=True)
class StoredCredentials:
    """An OAuth token set stored atomically in the OS keychain."""

    access_token: str
    refresh_token: str | None = None
    expires_at: float | None = None
    refresh_token_expires_at: float | None = None


class OAuthCredentialStore:
    """A secure keychain entry for one provider and one account identity."""

    def __init__(self, service: str, username: str) -> None:
        self._service = service
        self._username = username

    def _check_backend(self) -> None:
        try:
            priority = keyring.get_keyring().priority
        except (keyring.errors.NoKeyringError, NotImplementedError) as error:
            raise OAuthCredentialError("no secure keyring backend available on this system; the value cannot be stored safely") from error
        if priority < 1:
            raise OAuthCredentialError("no secure keyring backend available on this system; the value cannot be stored safely")

    def get_credentials(self) -> StoredCredentials | None:
        try:
            value = keyring.get_password(self._service, self._username)
        except keyring.errors.NoKeyringError:
            return None
        if not value:
            return None
        try:
            data = json.loads(value)
        except json.JSONDecodeError:
            return StoredCredentials(value)
        if not isinstance(data, dict) or not isinstance(data.get("access_token"), str):
            return StoredCredentials(value)
        return StoredCredentials(
            data["access_token"],
            _optional_string(data.get("refresh_token")),
            _optional_number(data.get("expires_at")),
            _optional_number(data.get("refresh_token_expires_at")),
        )

    def get(self) -> str | None:
        credentials = self.get_credentials()
        return credentials.access_token if credentials else None

    def set_credentials(self, credentials: StoredCredentials) -> None:
        self._check_backend()
        keyring.set_password(
            self._service,
            self._username,
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

    def set(self, token: str) -> None:
        self.set_credentials(StoredCredentials(token))

    def delete(self) -> None:
        try:
            keyring.delete_password(self._service, self._username)
        except (keyring.errors.PasswordDeleteError, keyring.errors.NoKeyringError):
            pass


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_number(value: object) -> float | None:
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def expiry_at(now: float, seconds: object) -> float | None:
    duration = optional_int(seconds)
    return now + duration if duration is not None else None
