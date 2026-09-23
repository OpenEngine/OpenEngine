"""Run progress reported as comments on the originating GitHub issue."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from engine.apps.web.github_communications import (
    ChannelRoutedCommunications,
    GithubCommunications,
    render_github_comment,
)
from engine.domain import RunId, RunOrigin, RunState, TaskId, WorkflowId
from engine.ports import CommentResult, Message, MessageLink
from engine.runtime.notifications import RunNotifier


def _source_control() -> MagicMock:
    return MagicMock(add_comment=AsyncMock(return_value=CommentResult(41, "https://c")))


def test_progress_is_a_new_comment_on_the_issue():
    source = _source_control()
    comms = GithubCommunications(source)

    posted = asyncio.run(comms.post(
        "github:acme/api",
        Message("*Review* started.", (
            MessageLink("View work order", "https://engine.example/runs/r1"),
            MessageLink("View pull request", "https://github.com/acme/api/pull/9"),
        )),
        thread_id="issue/7",
    ))

    assert posted == "41"
    source.add_comment.assert_awaited_once_with(
        "https://github.com/acme/api/pull/7",
        "**Review** started.\n\n[View work order](https://engine.example/runs/r1)"
        " · [View pull request](https://github.com/acme/api/pull/9)",
    )


def test_a_mention_addresses_the_person_who_assigned_the_issue():
    assert render_github_comment(Message("Work order failed: boom", mention="maintainer")) == (
        "@maintainer Work order failed: boom"
    )


def test_an_unaddressable_thread_is_refused():
    with pytest.raises(ValueError):
        asyncio.run(GithubCommunications(_source_control()).post("github:acme/api", "hi"))


def test_github_channels_route_to_github_and_the_rest_to_chat():
    chat = MagicMock(post=AsyncMock(return_value="slack-ts"))
    github = MagicMock(post=AsyncMock(return_value="41"))
    routed = ChannelRoutedCommunications(chat, github)
    notifier = RunNotifier(routed, "https://engine.example")

    def state(origin: RunOrigin) -> RunState:
        return RunState(
            run_id=RunId("r1"), task_id=TaskId("t1"),
            workflow_id=WorkflowId("implementation-review-v1"), origin=origin,
        )

    asyncio.run(notifier.deliver(
        state(RunOrigin("github:acme/api", "issue/7", "maintainer")), "failed", mention=True,
    ))
    asyncio.run(notifier.deliver(state(RunOrigin("C1", "1.0", "U1")), "started"))

    channel, message, _ = github.post.await_args.args
    assert (channel, message.text, message.mention) == ("github:acme/api", "failed", "maintainer")
    assert github.post.await_args.kwargs == {"thread_id": "issue/7"}
    assert chat.post.await_args.args[0] == "C1"
