"""The signed route GitHub comments arrive on.

Three things have to hold: an unsigned delivery starts nothing, a new comment
on an issue or a review thread is queued once no matter how often GitHub
redelivers it, and the acknowledgement does not wait for whatever answers it.
"""

import asyncio
import hashlib
import hmac
import json

import pytest

from engine.apps.web.github_ingress import (
    GithubIngress,
    GithubMerge,
    GithubReopen,
    comment_from_payload,
    merge_from_payload,
    reopen_from_payload,
    verify_signature,
)

WEBHOOK_SECRET = "shhh"


def _signed(body: bytes, secret: str = WEBHOOK_SECRET) -> dict[str, str]:
    return {
        "x-hub-signature-256": "sha256="
        + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
        "content-type": "application/json",
    }


def _issue_comment(comment_id: int = 1, body: str = "please fix it", **comment) -> dict:
    return {
        "action": "created",
        "issue": {"number": 7},
        "comment": dict(
            {"id": comment_id, "body": body, "html_url": "https://github.com/acme/api/issues/7#c",
             "user": {"login": "someone", "type": "User"}, "author_association": "COLLABORATOR"},
            **comment,
        ),
        "repository": {"full_name": "acme/api"},
    }


def _merged_pull_request(number: int = 7, **pull_request) -> dict:
    return {
        "action": "closed",
        "pull_request": dict(
            {"number": number, "merged": True,
             "merged_by": {"login": "maintainer", "type": "User"},
             "html_url": "https://github.com/acme/api/pull/7"},
            **pull_request,
        ),
        "repository": {"full_name": "acme/api"},
        "sender": {"login": "maintainer", "type": "User"},
    }


# --- reading a delivery ------------------------------------------------------


def test_a_new_issue_comment_is_read() -> None:
    comment = comment_from_payload("issue_comment", _issue_comment())
    assert comment is not None
    assert (comment.repository, comment.number, comment.author) == ("acme/api", 7, "someone")
    assert comment.body == "please fix it"
    assert comment.comment_id == "1"
    assert not comment.is_pull_request


def test_a_comment_on_a_pull_request_says_so() -> None:
    payload = _issue_comment()
    payload["issue"]["pull_request"] = {"url": "https://api.github.com/repos/acme/api/pulls/7"}
    comment = comment_from_payload("issue_comment", payload)
    assert comment is not None and comment.is_pull_request


def test_a_review_thread_reply_keeps_the_comment_it_answers() -> None:
    payload = {
        "action": "created",
        "pull_request": {"number": 12},
        "comment": {"id": 99, "body": "and this line", "in_reply_to_id": 98,
                    "user": {"login": "someone", "type": "User"},
                    "author_association": "MEMBER"},
        "repository": {"full_name": "acme/api"},
    }
    comment = comment_from_payload("pull_request_review_comment", payload)
    assert comment is not None
    assert (comment.number, comment.in_reply_to_id) == (12, "98")
    assert comment.is_pull_request


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        ("issues", _issue_comment()),  # not an event this route acts on
        ("issue_comment", dict(_issue_comment(), action="edited")),
        ("issue_comment", dict(_issue_comment(), action="deleted")),
        ("issue_comment", _issue_comment(user={"login": "engine[bot]", "type": "Bot"})),
        ("issue_comment", _issue_comment(author_association="NONE")),
        ("issue_comment", _issue_comment(author_association="CONTRIBUTOR")),
        ("issue_comment", _issue_comment(author_association="FIRST_TIME_CONTRIBUTOR")),
        ("issue_comment", _issue_comment(author_association=None)),
        ("issue_comment", {"action": "created", "comment": {"id": 1}}),
        ("issue_comment", dict(_issue_comment(), repository={})),
        ("issue_comment", dict(_issue_comment(), issue={"number": "7"})),
    ],
)
def test_deliveries_that_are_not_somebody_asking_for_something(event, payload) -> None:
    assert comment_from_payload(event, payload) is None


@pytest.mark.parametrize("association", ["OWNER", "MEMBER", "COLLABORATOR"])
def test_the_people_who_can_write_to_a_repository_can_direct_engine(association) -> None:
    payload = _issue_comment(author_association=association)
    assert comment_from_payload("issue_comment", payload) is not None


def test_engine_does_not_answer_its_own_comments() -> None:
    """Engine posts as a machine user, not a GitHub app: that account is an
    ordinary `User` and a collaborator, so only its login distinguishes it."""
    payload = _issue_comment(user={"login": "OpenEngine-worker", "type": "User"})
    assert comment_from_payload("issue_comment", payload) is not None
    assert comment_from_payload("issue_comment", payload, self_login="OpenEngine-worker") is None
    # GitHub logins are case-insensitive, so the comparison has to be too.
    assert comment_from_payload("issue_comment", payload, self_login="openengine-worker") is None
    # Somebody else's comment is still answered.
    assert comment_from_payload("issue_comment", _issue_comment(), self_login="OpenEngine-worker")


def test_a_github_app_is_still_recognised_by_its_user_type() -> None:
    payload = _issue_comment(user={"login": "engine[bot]", "type": "Bot"})
    assert comment_from_payload("issue_comment", payload, self_login="") is None


def test_a_merged_pull_request_is_read() -> None:
    merged = merge_from_payload("pull_request", _merged_pull_request())
    assert merged is not None
    assert (merged.repository, merged.number) == ("acme/api", 7)
    assert merged.merged_by == "maintainer"
    assert merged.url == "https://github.com/acme/api/pull/7"
    assert merged.merged


def test_a_pull_request_closed_unmerged_is_read_as_a_rejection() -> None:
    """GitHub names no `merged_by` for a close, so who closed it is the sender."""
    payload = _merged_pull_request(merged=False, merged_by=None)
    payload["sender"] = {"login": "reviewer", "type": "User"}
    closed = merge_from_payload("pull_request", payload)
    assert closed is not None
    assert (closed.repository, closed.number, closed.merged_by) == ("acme/api", 7, "reviewer")
    assert not closed.merged


@pytest.mark.parametrize(
    "sender",
    [
        # A stale-branch sweeper or Engine's own account has judged nothing.
        {"login": "stale[bot]", "type": "Bot"},
        {"login": "openengine-bot", "type": "User"},
        None,
        {"login": "", "type": "User"},
    ],
)
def test_a_close_by_somebody_who_may_not_decide_is_ignored(sender) -> None:
    payload = dict(_merged_pull_request(merged=False, merged_by=None), sender=sender)
    assert merge_from_payload(
        "pull_request", payload, self_login="OpenEngine-Bot"
    ) is None


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        # GitHub always says whether a closed pull request merged.
        ("pull_request", _merged_pull_request(merged=None)),
        # Anything else in a pull request's lifecycle decides nothing.
        ("pull_request", dict(_merged_pull_request(), action="opened")),
        ("pull_request", dict(_merged_pull_request(), action="synchronize")),
        ("pull_request", dict(_merged_pull_request(), repository={})),
        ("pull_request", _merged_pull_request(number="7")),
        ("issue_comment", _issue_comment()),
        # An approving review is not read: the branch can take more commits and
        # another round of review after one, so it is not the point the work
        # order's pull request is closed out.
        ("pull_request_review", {"action": "submitted",
                                 "review": {"id": 5, "state": "approved",
                                            "user": {"login": "maintainer", "type": "User"},
                                            "author_association": "COLLABORATOR"},
                                 "pull_request": {"number": 7},
                                 "repository": {"full_name": "acme/api"}}),
    ],
)
def test_deliveries_that_are_not_a_merge(event, payload) -> None:
    assert merge_from_payload(event, payload) is None


@pytest.mark.parametrize(
    "pull_request",
    [
        # The gate asks for a person to read the diff, and write access is not
        # that property: a merge queue, an auto-merge firing on green CI, a
        # Dependabot-style app, or Engine's own App all hold it and none of
        # them has read anything.
        {"merged_by": {"login": "github-merge-queue[bot]", "type": "Bot"}},
        {"merged_by": {"login": "openengine-bot", "type": "User"}},
        # A merge Engine cannot attribute to a person is not a person having
        # reviewed the work.
        {"merged_by": None},
        {"merged_by": {"type": "User"}},
        {"merged_by": {"login": "", "type": "User"}},
    ],
)
def test_a_merge_by_somebody_who_may_not_decide_is_ignored(pull_request) -> None:
    payload = _merged_pull_request(**pull_request)
    assert merge_from_payload(
        "pull_request", payload, self_login="OpenEngine-Bot"
    ) is None


def test_a_comment_and_a_merge_are_told_apart() -> None:
    """One reader per kind: neither payload is the other's shape."""
    assert comment_from_payload("pull_request", _merged_pull_request()) is None
    assert merge_from_payload("issue_comment", _issue_comment()) is None


def test_only_our_secret_signs_a_delivery() -> None:
    body = json.dumps(_issue_comment()).encode()
    assert verify_signature(WEBHOOK_SECRET, _signed(body)["x-hub-signature-256"], body)
    assert not verify_signature(WEBHOOK_SECRET, _signed(body, "wrong")["x-hub-signature-256"], body)
    assert not verify_signature(WEBHOOK_SECRET, _signed(body)["x-hub-signature-256"], body + b" ")
    assert not verify_signature("", _signed(body)["x-hub-signature-256"], body)
    assert not verify_signature(WEBHOOK_SECRET, "", body)


# --- the queue behind the route ----------------------------------------------


def _record(handled):
    async def handle(comment):
        handled.append(comment)

    return handle


def test_a_comment_is_handled_once_however_often_it_is_delivered() -> None:
    async def scenario():
        handled = []
        ingress = GithubIngress(repository="acme/api", webhook_secret=lambda: WEBHOOK_SECRET, handle=_record(handled))
        assert ingress.accept("issue_comment", _issue_comment(comment_id=1))
        assert ingress.accept("issue_comment", _issue_comment(comment_id=1))
        assert ingress.accept("issue_comment", _issue_comment(comment_id=2, body="again"))
        await ingress.drain()
        assert [c.comment_id for c in handled] == ["1", "2"]
        await ingress.close()

    asyncio.run(scenario())


def test_a_full_queue_asks_github_to_redeliver() -> None:
    async def scenario():
        gate = asyncio.Event()
        handled = []

        async def handle(comment):
            handled.append(comment)
            await gate.wait()

        ingress = GithubIngress(repository="acme/api", webhook_secret=lambda: WEBHOOK_SECRET, handle=handle, capacity=1)
        assert ingress.accept("issue_comment", _issue_comment(comment_id=1))
        await asyncio.sleep(0)  # the worker takes the first comment off the queue
        assert ingress.accept("issue_comment", _issue_comment(comment_id=2))
        assert not ingress.accept("issue_comment", _issue_comment(comment_id=3))
        gate.set()
        await ingress.drain()
        # The refused delivery is not remembered, so GitHub's retry is taken.
        assert ingress.accept("issue_comment", _issue_comment(comment_id=3))
        await ingress.drain()
        assert [c.comment_id for c in handled] == ["1", "2", "3"]
        await ingress.close()

    asyncio.run(scenario())


def test_nothing_is_queued_with_nothing_to_answer_it() -> None:
    ingress = GithubIngress(repository="acme/api", webhook_secret=lambda: WEBHOOK_SECRET)
    assert not ingress.accept("issue_comment", _issue_comment())
    # A delivery this route never acts on is still settled, handler or not.
    assert ingress.accept("issues", _issue_comment())


def test_a_failed_comment_can_be_redelivered() -> None:
    """A handler that raises must not consume the comment: the delivery was
    already acknowledged, so deduplicating the retry away would lose the work."""

    async def scenario():
        attempts = []

        async def handle(comment):
            attempts.append(comment.comment_id)
            if len(attempts) == 1:
                raise RuntimeError("agent is down")

        ingress = GithubIngress(repository="acme/api", webhook_secret=lambda: WEBHOOK_SECRET, handle=handle)
        assert ingress.accept("issue_comment", _issue_comment(comment_id=1))
        await ingress.drain()
        assert ingress.accept("issue_comment", _issue_comment(comment_id=1))
        await ingress.drain()
        assert attempts == ["1", "1"]
        # Once it succeeds it is remembered again, so a third copy is dropped.
        assert ingress.accept("issue_comment", _issue_comment(comment_id=1))
        await ingress.drain()
        assert attempts == ["1", "1"]
        await ingress.close()

    asyncio.run(scenario())


def test_a_failing_handler_does_not_stop_the_next_comment() -> None:
    async def scenario():
        handled = []

        async def handle(comment):
            handled.append(comment)
            raise RuntimeError("agent is down")

        ingress = GithubIngress(repository="acme/api", webhook_secret=lambda: WEBHOOK_SECRET, handle=handle)
        ingress.accept("issue_comment", _issue_comment(comment_id=1))
        await ingress.drain()
        ingress.accept("issue_comment", _issue_comment(comment_id=2))
        await ingress.drain()
        assert [c.comment_id for c in handled] == ["1", "2"]
        await ingress.close()

    asyncio.run(scenario())


def test_a_merge_is_handled_once_however_often_it_is_delivered() -> None:
    async def scenario():
        merges = []
        ingress = GithubIngress(
            repository="acme/api", webhook_secret=lambda: WEBHOOK_SECRET,
            handle=_record([]), handle_merge=_record(merges),
        )
        assert ingress.accept("pull_request", _merged_pull_request(number=7))
        assert ingress.accept("pull_request", _merged_pull_request(number=7))
        assert ingress.accept("pull_request", _merged_pull_request(number=8))
        await ingress.drain()
        assert [m.number for m in merges] == [7, 8]
        await ingress.close()

    asyncio.run(scenario())


def test_a_reopened_pull_request_is_read() -> None:
    reopened = reopen_from_payload(
        "pull_request", dict(_merged_pull_request(merged=False), action="reopened")
    )
    assert reopened == GithubReopen(repository="acme/api", number=7)
    assert reopen_from_payload("pull_request", _merged_pull_request()) is None


def test_a_close_after_a_reopen_is_handled_again() -> None:
    """Close, reopen, close: the second close is a verdict, not a duplicate."""

    async def scenario():
        handled = []
        ingress = GithubIngress(
            repository="acme/api", webhook_secret=lambda: WEBHOOK_SECRET,
            handle=_record([]), handle_merge=_record(handled),
        )
        closed = _merged_pull_request(merged=False, merged_by=None)
        reopened = dict(closed, action="reopened")
        for payload in (closed, reopened, closed, reopened):
            assert ingress.accept("pull_request", payload)
        await ingress.drain()
        assert [type(delivery) for delivery in handled] == [
            GithubMerge, GithubReopen, GithubMerge, GithubReopen,
        ]
        await ingress.close()

    asyncio.run(scenario())


def test_a_merge_is_refused_while_nothing_is_wired_to_act_on_it() -> None:
    ingress = GithubIngress(
        repository="acme/api", webhook_secret=lambda: WEBHOOK_SECRET, handle=_record([]),
    )
    assert not ingress.accept("pull_request", _merged_pull_request())
    # Closing without merging is a verdict too, so it is not dropped either.
    assert not ingress.accept("pull_request", _merged_pull_request(merged=False))
    # Anything else in a pull request's lifecycle is settled either way.
    assert ingress.accept("pull_request", dict(_merged_pull_request(), action="opened"))


def test_a_merge_from_another_repository_is_ignored() -> None:
    merges = []
    ingress = GithubIngress(
        repository="other/api", webhook_secret=lambda: WEBHOOK_SECRET,
        handle=_record([]), handle_merge=_record(merges),
    )
    assert ingress.accept("pull_request", _merged_pull_request())
    assert merges == []


def test_a_failed_merge_can_be_redelivered() -> None:
    """The verdict a merge carries is not something to drop on one failure."""

    async def scenario():
        attempts = []

        async def handle_merge(merged):
            attempts.append(merged.number)
            if len(attempts) == 1:
                raise RuntimeError("the graph engine is down")

        ingress = GithubIngress(
            repository="acme/api", webhook_secret=lambda: WEBHOOK_SECRET,
            handle=_record([]), handle_merge=handle_merge,
        )
        assert ingress.accept("pull_request", _merged_pull_request())
        await ingress.drain()
        assert ingress.accept("pull_request", _merged_pull_request())
        await ingress.drain()
        assert attempts == [7, 7]
        # Once it succeeds it is remembered again, so a third copy is dropped.
        assert ingress.accept("pull_request", _merged_pull_request())
        await ingress.drain()
        assert attempts == [7, 7]
        await ingress.close()

    asyncio.run(scenario())


def test_a_merge_does_not_take_a_row_on_the_comment_panel() -> None:
    """The panel is a window on comments somebody is waiting for an answer to;
    a merge is a verdict this process acts on, not a conversation."""

    async def scenario():
        from engine.apps.web.github_activity import GithubActivityLog

        activity = GithubActivityLog()
        ingress = GithubIngress(
            repository="acme/api", webhook_secret=lambda: WEBHOOK_SECRET,
            handle=_record([]), handle_merge=_record([]), activity=activity,
        )
        assert ingress.accept("pull_request", _merged_pull_request())
        await ingress.drain()
        assert activity.recent() == ()
        await ingress.close()

    asyncio.run(scenario())


# --- the route ---------------------------------------------------------------


_UNWIRED = object()


def _client(secret: str = WEBHOOK_SECRET, handle=None, repository: str = "acme/api"):
    """A test client over the route. Handles comments into nowhere by default;
    pass ``handle=_UNWIRED`` for the app as it stands with nothing wired."""
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    if handle is _UNWIRED:
        handle = None
    elif handle is None:
        handle = _record([])
    ingress = GithubIngress(repository=repository, webhook_secret=lambda: secret, handle=handle)
    app = Starlette(routes=[Route("/api/github/events", ingress.webhook, methods=["POST"])])
    return TestClient(app), ingress


def test_an_unsigned_delivery_is_refused() -> None:
    client, ingress = _client()
    body = json.dumps(_issue_comment()).encode()
    assert client.post("/api/github/events", content=body).status_code == 401
    assert client.post(
        "/api/github/events", content=body, headers=_signed(body, "wrong")
    ).status_code == 401


def test_without_a_secret_the_route_reports_it_is_not_configured() -> None:
    client, _ingress = _client(secret="")
    body = json.dumps(_issue_comment()).encode()
    assert client.post("/api/github/events", content=body, headers=_signed(body)).status_code == 503


def test_a_comment_is_refused_while_nothing_is_wired_to_answer_it() -> None:
    """The window before a handler lands loses no comment: GitHub records the
    delivery as failed, and it can be redelivered once one is wired."""
    client, _ingress = _client(handle=_UNWIRED)
    body = json.dumps(_issue_comment()).encode()
    headers = dict(_signed(body), **{"x-github-event": "issue_comment"})
    assert client.post("/api/github/events", content=body, headers=headers).status_code == 503


def test_an_event_this_route_ignores_is_acknowledged_with_nothing_wired() -> None:
    """A webhook subscribed to more events than Engine reads must not see every
    one of them fail and retry forever."""
    client, _ingress = _client(handle=_UNWIRED)
    body = json.dumps({"action": "opened", "issue": {"number": 7}}).encode()
    for event in ("issues", "push", "star"):
        response = client.post(
            "/api/github/events", content=body, headers=dict(_signed(body), **{"x-github-event": event})
        )
        assert response.status_code == 200, event


def test_the_webhook_can_be_saved_before_anything_answers_it() -> None:
    client, _ingress = _client(handle=_UNWIRED)
    body = json.dumps({"zen": "Design for failure."}).encode()
    response = client.post(
        "/api/github/events", content=body, headers=dict(_signed(body), **{"x-github-event": "ping"})
    )
    assert response.status_code == 200


def test_a_delivery_too_large_to_buffer_is_refused_before_it_is_read() -> None:
    """The route is reachable without a session, so an unsigned body cannot be
    a way to spend this process's memory."""
    client, _ingress = _client()
    body = b'{"padding": "' + b"x" * (2 * 1024 * 1024) + b'"}'
    response = client.post(
        "/api/github/events", content=body, headers=dict(_signed(body), **{"x-github-event": "issue_comment"})
    )
    assert response.status_code == 413


def test_a_body_larger_than_it_declares_is_refused() -> None:
    """A chunked body has no declared length to check, so the limit is also
    enforced while the body streams in."""
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    ingress = GithubIngress(
        webhook_secret=lambda: WEBHOOK_SECRET, handle=_record([]), max_body_bytes=64
    )
    app = Starlette(routes=[Route("/api/github/events", ingress.webhook, methods=["POST"])])
    client = TestClient(app)

    def chunks():
        for _ in range(8):
            yield b"x" * 32

    # httpx sends a generator body with Transfer-Encoding: chunked, so no
    # Content-Length is declared and the streaming limit is what catches it.
    assert client.post("/api/github/events", content=chunks()).status_code == 413


def test_a_lying_content_length_is_refused() -> None:
    client, _ingress = _client()
    body = json.dumps(_issue_comment()).encode()
    headers = dict(_signed(body), **{"x-github-event": "issue_comment"})
    assert client.post(
        "/api/github/events", content=body, headers=dict(headers, **{"content-length": "not a number"})
    ).status_code == 413


def test_an_ordinary_comment_is_well_under_the_limit() -> None:
    handled = []
    client, ingress = _client(handle=_record(handled))
    body = json.dumps(_issue_comment(comment_id=11)).encode()
    headers = dict(_signed(body), **{"x-github-event": "issue_comment"})
    with client:
        assert client.post("/api/github/events", content=body, headers=headers).status_code == 200
        client.portal.call(ingress.drain)
        client.portal.call(ingress.close)
    assert [c.comment_id for c in handled] == ["11"]


def test_the_ping_that_saves_the_webhook_is_answered() -> None:
    client, _ingress = _client()
    body = json.dumps({"zen": "Design for failure."}).encode()
    response = client.post(
        "/api/github/events", content=body, headers=dict(_signed(body), **{"x-github-event": "ping"})
    )
    assert response.status_code == 200 and response.json() == {"ok": True}


def test_a_body_that_is_not_an_event_is_rejected() -> None:
    client, _ingress = _client()
    for body in (b"not json", b"[]"):
        response = client.post(
            "/api/github/events",
            content=body,
            headers=dict(_signed(body), **{"x-github-event": "issue_comment"}),
        )
        assert response.status_code == 400


def test_a_signed_comment_is_acknowledged_and_queued() -> None:
    handled = []
    client, ingress = _client(handle=_record(handled))
    body = json.dumps(_issue_comment(comment_id=5)).encode()
    headers = dict(_signed(body), **{"x-github-event": "issue_comment"})
    with client:
        response = client.post("/api/github/events", content=body, headers=headers)
        assert client.post("/api/github/events", content=body, headers=headers).status_code == 200
        client.portal.call(ingress.drain)
        client.portal.call(ingress.close)
    assert response.status_code == 200
    assert [c.comment_id for c in handled] == ["5"]


def test_the_webhook_route_is_exempt_from_session_auth() -> None:
    from engine.apps.web.github_login import _AUTH_EXEMPT

    assert "/api/github/events" in _AUTH_EXEMPT


@pytest.mark.parametrize("event", ["issue_comment", "pull_request_review_comment"])
@pytest.mark.parametrize("repository", ["acme/api", "ACME/API", "other/api", ""])
def test_signed_deliveries_only_queue_for_the_configured_repository(event, repository) -> None:
    handled = []
    client, ingress = _client(handle=_record(handled), repository=repository)
    payload = _issue_comment()
    if event == "pull_request_review_comment":
        payload["pull_request"] = payload.pop("issue")
    body = json.dumps(payload).encode()
    headers = dict(_signed(body), **{"x-github-event": event})
    with client:
        response = client.post("/api/github/events", content=body, headers=headers)
        client.portal.call(ingress.drain)
        client.portal.call(ingress.close)
    assert response.status_code == (503 if not repository else 200)
    assert [c.repository for c in handled] == (
        ["acme/api"] if repository.lower() == "acme/api" else []
    )


def _assigned_issue(**issue) -> dict:
    return {
        "action": "assigned",
        "repository": {"full_name": "acme/api"},
        "assignee": {"login": "OpenEngineBot"},
        "sender": {"login": "maintainer"},
        "issue": dict(number=7, state="open", title="Fix the bug", body="Reproduction steps",
                      html_url="https://github.com/acme/api/issues/7", **issue),
    }


def test_assignment_reads_issue_and_assigning_actor():
    from engine.apps.web.github_ingress import assignment_from_payload

    assigned = assignment_from_payload("issues", _assigned_issue(), self_login="openenginebot")
    assert assigned is not None
    assert (assigned.repository, assigned.number, assigned.assignee, assigned.sender) == (
        "acme/api", 7, "OpenEngineBot", "maintainer")
    assert (assigned.title, assigned.body) == ("Fix the bug", "Reproduction steps")


@pytest.mark.parametrize("change", [
    {"action": "opened"}, {"action": "unassigned"},
    {"assignee": {"login": "someone"}}, {"assignee": None}, {"sender": {}},
    {"issue": {"number": True, "state": "open"}},
    {"issue": {"number": 7, "state": "closed"}},
    {"issue": {"number": 7, "state": "open", "pull_request": {}}},
])
def test_other_assignments_are_ignored(change):
    from engine.apps.web.github_ingress import assignment_from_payload

    assert assignment_from_payload(
        "issues", dict(_assigned_issue(), **change), self_login="OpenEngineBot",
    ) is None


def test_assignments_require_configured_login():
    from engine.apps.web.github_ingress import assignment_from_payload

    assert assignment_from_payload("issues", _assigned_issue()) is None


def test_assignment_queue_deduplicates_and_retries_failures():
    async def scenario():
        calls = []

        async def handle(assignment):
            calls.append(assignment)
            if len(calls) == 1:
                raise RuntimeError("temporarily unavailable")

        ingress = GithubIngress(repository="acme/api", self_login=lambda: "OpenEngineBot",
                                handle_assignment=handle)
        try:
            assert ingress.accept("issues", _assigned_issue())
            assert ingress.accept("issues", _assigned_issue())
            await ingress.drain()
            assert len(calls) == 1
            assert ingress.accept("issues", _assigned_issue())
            await ingress.drain()
            assert ingress.accept("issues", _assigned_issue())
            await ingress.drain()
            assert len(calls) == 2
            assert ingress.accept("issues", dict(_assigned_issue(), repository={"full_name": "other/repo"}))
            await ingress.drain()
            assert len(calls) == 2
        finally:
            await ingress.close()

    asyncio.run(scenario())
