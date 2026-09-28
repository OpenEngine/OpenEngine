"""Reporting a run's progress on the GitHub issue or pull request it came from.

A run started by assigning an issue carries a ``github:<owner>/<repo>`` origin,
and its progress belongs on that issue's timeline. Each update is a new comment,
so the timeline reads as the run's history; nothing is edited in place, and no
comment id is kept.
"""

from __future__ import annotations

import re

from engine.domain.ids import RunId
from engine.ports import Communications, Message, SourceControl
from engine.runtime.change_requests import pull_request_url

GITHUB_CHANNEL_PREFIX = "github:"

# Callers write Slack's single-asterisk bold; GitHub reads that as italics.
_SLACK_BOLD = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?![*\w])")


def render_github_comment(message: str | Message) -> str:
    """The Markdown body of one progress comment."""
    if isinstance(message, str):
        message = Message(message)
    text = _SLACK_BOLD.sub(r"**\1**", message.text)
    if message.mention:
        text = f"@{message.mention} {text}"
    if message.links:
        text += "\n\n" + " · ".join(f"[{link.label}]({link.url})" for link in message.links)
    return text


class GithubCommunications:
    """Post messages as comments on a GitHub issue or pull request.

    `channel` is ``github:<owner>/<repo>`` and `thread_id` is ``issue/<number>``
    or a pull request number. GitHub's issue-comments endpoint serves both.
    """

    def __init__(self, source_control: SourceControl) -> None:
        self._source_control = source_control

    async def post(
        self,
        channel: str,
        message: str | Message,
        run_id: RunId | None = None,
        thread_id: str = "",
    ) -> str:
        repository = channel.removeprefix(GITHUB_CHANNEL_PREFIX)
        number = thread_id.removeprefix("issue/").partition("/review/")[0]
        if not repository or not number.isdigit():
            raise ValueError(f"cannot address a GitHub comment to {channel!r} {thread_id!r}")
        result = await self._source_control.add_comment(
            pull_request_url(repository, int(number)), render_github_comment(message),
        )
        return str(result.id)

    async def reply(self, message_id: str, message: str) -> str:
        raise NotImplementedError("GitHub progress comments are never threaded")


class ChannelRoutedCommunications:
    """Send ``github:`` channels to GitHub and everything else to the chat provider."""

    def __init__(self, chat: Communications, github: Communications) -> None:
        self._chat = chat
        self._github = github

    def _for(self, channel: str) -> Communications:
        return self._github if channel.startswith(GITHUB_CHANNEL_PREFIX) else self._chat

    async def post(
        self,
        channel: str,
        message: str | Message,
        run_id: RunId | None = None,
        thread_id: str = "",
    ) -> str:
        return await self._for(channel).post(channel, message, run_id, thread_id=thread_id)

    async def reply(self, message_id: str, message: str) -> str:
        return await self._chat.reply(message_id, message)

    def __getattr__(self, name: str) -> object:
        # Provider extras such as Slack's reactions stay reachable.
        return getattr(self._chat, name)


__all__ = [
    "ChannelRoutedCommunications",
    "GithubCommunications",
    "render_github_comment",
]
