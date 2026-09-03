"""Slack OAuth V2 flow and secure credential storage."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import keyring

_SERVICE = "openengine"
_CLIENT_ID = "slack-client-id"
_CLIENT_SECRET = "slack-client-secret"
_ACCESS_TOKEN = "slack-access-token"
_TOKEN_URL = "https://slack.com/api/oauth.v2.access"
_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"


class SlackAuthError(RuntimeError):
    """Slack authorization or secure storage failed."""


@dataclass(frozen=True, slots=True)
class SlackCredentials:
    client_id: str
    client_secret: str


class SlackCredentialStore:
    def _check_backend(self) -> None:
        try:
            if keyring.get_keyring().priority < 1:
                raise SlackAuthError("no secure keyring backend available on this system")
        except (keyring.errors.NoKeyringError, NotImplementedError):
            raise SlackAuthError("no secure keyring backend available on this system")

    def credentials(self) -> SlackCredentials | None:
        try:
            client_id = keyring.get_password(_SERVICE, _CLIENT_ID)
            client_secret = keyring.get_password(_SERVICE, _CLIENT_SECRET)
        except keyring.errors.NoKeyringError:
            return None
        if not client_id or not client_secret:
            return None
        return SlackCredentials(client_id, client_secret)

    def set_credentials(self, client_id: str, client_secret: str) -> None:
        self._check_backend()
        keyring.set_password(_SERVICE, _CLIENT_ID, client_id)
        keyring.set_password(_SERVICE, _CLIENT_SECRET, client_secret)
        # A token belongs to the app that issued it. Changing apps requires a
        # fresh authorization rather than retaining a misleading connection.
        self.disconnect()

    def token(self) -> str | None:
        try:
            return keyring.get_password(_SERVICE, _ACCESS_TOKEN)
        except keyring.errors.NoKeyringError:
            return None

    def set_token(self, token: str) -> None:
        self._check_backend()
        keyring.set_password(_SERVICE, _ACCESS_TOKEN, token)

    def disconnect(self) -> None:
        for username in (_ACCESS_TOKEN,):
            try:
                keyring.delete_password(_SERVICE, username)
            except (keyring.errors.PasswordDeleteError, keyring.errors.NoKeyringError):
                pass


def authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    return _AUTHORIZE_URL + "?" + urlencode(
        {
            "client_id": client_id,
            "scope": "chat:write",
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )


async def exchange_code(credentials: SlackCredentials, code: str, redirect_uri: str) -> str:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            _TOKEN_URL,
            data={"code": code, "redirect_uri": redirect_uri},
            auth=(credentials.client_id, credentials.client_secret),
        )
    if response.is_error:
        raise SlackAuthError(f"Slack returned {response.status_code} during authorization")
    body = response.json()
    if not body.get("ok") or not body.get("access_token"):
        raise SlackAuthError(f"Slack authorization failed: {body.get('error', 'no access token')}")
    return str(body["access_token"])


__all__ = ["SlackAuthError", "SlackCredentialStore", "authorization_url", "exchange_code"]
