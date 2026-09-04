"""Slack OAuth V2 flow and secure credential storage."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
import keyring

_SERVICE = "openengine"
_CLIENT_ID = "slack-client-id"
_CLIENT_SECRET = "slack-client-secret"
_ACCESS_TOKEN = "slack-access-token"
_TOKEN_URL = "https://slack.com/api/oauth.v2.access"
_REVOKE_URL = "https://slack.com/api/auth.revoke"
_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"
_LIST_CONVERSATIONS_URL = "https://slack.com/api/conversations.list"


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
        previous = {
            _CLIENT_ID: keyring.get_password(_SERVICE, _CLIENT_ID),
            _CLIENT_SECRET: keyring.get_password(_SERVICE, _CLIENT_SECRET),
        }
        try:
            keyring.set_password(_SERVICE, _CLIENT_ID, client_id)
            keyring.set_password(_SERVICE, _CLIENT_SECRET, client_secret)
        except keyring.errors.KeyringError as error:
            for username, value in previous.items():
                try:
                    if value is None:
                        keyring.delete_password(_SERVICE, username)
                    else:
                        keyring.set_password(_SERVICE, username, value)
                except keyring.errors.KeyringError:
                    pass
            raise SlackAuthError("could not securely save Slack credentials") from error
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


class SlackCommunications:
    """Deliver Engine notifications through the connected Slack workspace."""

    def __init__(self, credential_store: SlackCredentialStore) -> None:
        self._credential_store = credential_store

    async def post(self, channel: str, message: str, run_id=None) -> str:
        token = self._credential_store.token()
        if not token:
            return ""
        async with httpx.AsyncClient() as client:
            channel_id = await self._resolve_channel(client, token, channel)
            response = await client.post(
                _POST_MESSAGE_URL,
                headers={"Authorization": f"Bearer {token}"},
                json={"channel": channel_id, "text": message},
            )
        if response.is_error:
            raise SlackAuthError(
                f"Slack returned {response.status_code} while posting a notification"
            )
        body = response.json()
        if not body.get("ok"):
            raise SlackAuthError(
                f"Slack notification failed: {body.get('error', 'message was not sent')}"
            )
        return str(body.get("ts", ""))

    async def _resolve_channel(
        self, client: httpx.AsyncClient, token: str, channel: str
    ) -> str:
        if re.fullmatch(r"[CGD][A-Z0-9]{8,}", channel):
            return channel
        cursor = ""
        while True:
            params = {"types": "public_channel", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            response = await client.get(
                _LIST_CONVERSATIONS_URL,
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )
            body = response.json()
            if response.is_error or not body.get("ok"):
                raise SlackAuthError(
                    "Slack channel lookup failed: "
                    f"{body.get('error', response.status_code)}"
                )
            match = next(
                (
                    item
                    for item in body.get("channels", [])
                    if str(item.get("name", "")).casefold() == channel.casefold()
                ),
                None,
            )
            if match is not None:
                return str(match["id"])
            cursor = str(body.get("response_metadata", {}).get("next_cursor", ""))
            if not cursor:
                raise SlackAuthError(f"Slack channel not found: {channel}")

    async def reply(self, message_id: str, message: str) -> str:
        raise NotImplementedError("Slack notification threads are not supported")


def authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    return _AUTHORIZE_URL + "?" + urlencode(
        {
            "client_id": client_id,
            "scope": "chat:write,chat:write.public,channels:read",
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


async def revoke_token(token: str) -> None:
    async with httpx.AsyncClient() as client:
        response = await client.post(_REVOKE_URL, headers={"Authorization": f"Bearer {token}"})
    if response.is_error:
        raise SlackAuthError(f"Slack returned {response.status_code} while disconnecting")
    body = response.json()
    if not body.get("ok"):
        raise SlackAuthError(f"Slack disconnect failed: {body.get('error', 'token was not revoked')}")


__all__ = [
    "SlackAuthError",
    "SlackCommunications",
    "SlackCredentialStore",
    "authorization_url",
    "exchange_code",
    "revoke_token",
]
