"""One reading of a change-request URL, for everyone who has to agree on it.

A `pr_url` is passed between the step that reports it, the guard that decides
whether the run may report it, the gate that waits on its CI and the store that
writes down whose work it is. Each of those has to name the same pull request
from the same string, and when two of them read it differently the run waits on
one change request while reporting the verdict of another. They have disagreed
twice -- once over a path carrying two markers, once over what spells a number
-- so there is one reading here rather than one per caller. The adapters that
send these projects and numbers to a forge API read them from here too, so the
string a guard approved and the request that goes out cannot come apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit


_MERGE_REQUESTS = "/-/merge_requests/"


@dataclass(frozen=True, slots=True)
class ChangeRequest:
    """A pull request or merge request, as named by the URL it was read from.

    `project` namespaces the number so two forges counting from their own
    counters cannot collide: github.com keeps its bare `owner/repo` keys, which
    is what every record written so far is keyed by, and everything else is
    prefixed by the authority it lives on.

    The rest is what an adapter needs to address the change request, and is
    carried here so that the adapter sending a request does not read the URL a
    second time to recover it: `project` is lowercased and namespaced, so it
    names the change request but cannot be pasted into an API path. None of it
    takes part in equality -- two spellings of one change request are one.
    """

    project: str
    number: int
    kind: Literal["pull", "merge_request"] = field(default="pull", compare=False)
    #: The host the URL named, lowercased and without a port.
    host: str = field(default="", compare=False)
    #: The project path as the URL wrote it: `owner/repo` for a pull request,
    #: the whole nested path for a merge request.
    path: str = field(default="", compare=False)


def change_request(url: str) -> ChangeRequest | None:
    """Name the change request `url` points at, or nothing if it names none.

    Nothing is the answer for a URL that names more than one, too. A path
    carrying a second marker -- `/acme/app/pull/12/x/victim/repo/pull/99` --
    is `#12` read from the front and `#99` read from the back, and one string
    standing for two change requests is the misbinding every caller here is
    guarding against, so it is not a name at all.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        return None
    hostname = parsed.hostname.lower()
    authority = hostname
    port = parsed.port
    if port is not None and port != (443 if parsed.scheme == "https" else 80):
        authority = f"{hostname}:{port}"
    path = parsed.path
    if _MERGE_REQUESTS in path:
        return _merge_request(authority, hostname, path)
    return _pull_request(authority, hostname, path)


def _pull_request(authority: str, hostname: str, path: str) -> ChangeRequest | None:
    """Read `owner/repo/pull/<number>`, following renames and normalising case.

    Segments past the number are the forge's own views of the one pull request
    -- `/files`, `/commits` -- and are read past, but another `pull` among them
    is a second pull request and is refused. A repository or owner *named*
    `pull` sits before the marker, not after it, so `wei/pull/pull/123` is the
    one pull request it looks like.
    """
    segments = path.strip("/").split("/")
    number = change_request_number(segments[3]) if len(segments) >= 4 else None
    if (
        number is None
        or "-" in segments
        or "pull" in segments[4:]
        or not all(names_a_project_step(one) for one in segments[:2])
        or segments[2] != "pull"
    ):
        return None
    written = f"{segments[0]}/{segments[1]}"
    project = written.lower()
    if authority != "github.com":
        project = f"{authority}/{project}"
    return ChangeRequest(project, number, "pull", hostname, written)


def _merge_request(authority: str, hostname: str, path: str) -> ChangeRequest | None:
    """Read `<project path>/-/merge_requests/<iid>`.

    A GitLab project is nested to any depth, so the project is whatever stands
    before the first marker and the number must be the whole of what follows:
    a second marker leaves a remainder, and is refused by that alone.

    A `pull/<number>` inside the project path is refused for the same reason a
    second marker is -- `/x/y/pull/7/-/merge_requests/1` is one change request
    to whoever reads pull requests and another to whoever reads merge requests.
    A project or group merely *named* `pull` carries no number after it and is
    the merge request it looks like.
    """
    project, _, tail = path.partition(_MERGE_REQUESTS)
    iid, separator, remainder = tail.partition("/")
    number = change_request_number(iid)
    segments = project.strip("/").split("/")
    doubled = any(
        one == "pull" and change_request_number(following) is not None
        for one, following in zip(segments, segments[1:])
    )
    named = bool(segments) and all(names_a_project_step(one) for one in segments)
    if not named or number is None or separator or remainder or doubled:
        return None
    written = "/".join(segments)
    return ChangeRequest(
        f"{authority}/{written.lower()}", number, "merge_request", hostname, written
    )


def names_a_project_step(segment: str) -> bool:
    """Whether a path segment names one step of a project, rather than moves.

    `.` and `..` are not names. They are instructions about the path they sit
    in, and every reader that carries them out reads a different path than the
    one written down -- including `httpx`, which normalises them when a step is
    pasted into an API address. `https://github.com/../x/pull/1` then leaves as
    a request to `/x/issues/1/comments`, which is not a repository endpoint at
    all, so the request the code reads is not the request that is sent.

    Refused here and not only where a URL is built, so that a project nobody
    can address is not a project any reader calls the run's work either.
    """
    return bool(segment) and segment not in (".", "..")


def change_request_number(segment: str) -> int | None:
    """Read a change-request number the way every reader of one must.

    `str.isdigit` is true of `١٢` and of `²`, and a leading zero is a number to
    a parser reading digits and not to one matching `[1-9][0-9]*`, so each of
    those spellings is a change request to one reader and not to another. `²`
    is worse than a disagreement: `int` raises on it, so a URL spelled that way
    leaves by way of an exception rather than an answer either way.
    """
    if not segment.isascii() or not segment.isdigit() or segment.startswith("0"):
        return None
    return int(segment)
