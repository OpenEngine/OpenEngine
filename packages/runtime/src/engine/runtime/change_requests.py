"""One reading of a change-request URL, for everyone who has to agree on it.

A `pr_url` is passed between the step that reports it, the gate that waits on
its CI, the store that writes down whose work it is and the adapter that posts
to it. Each of those has to name the same change request from the same string,
and when two of them read it differently the run is bound to one change request
while it waits on another's CI. They have disagreed over a path carrying two
markers, over what spells a number, over a repository named `pull` and over
view suffixes such as `/files` -- so there is one reading here rather than one
per caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit


_MERGE_REQUESTS = "/-/merge_requests/"
#: Digits enough for any counter a forge keeps, and few enough that `int` reads
#: them rather than refusing the conversion.
_MOST_DIGITS = 19


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
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        # An authority the standard reader refuses -- an unclosed `[`, a port
        # that is not a number -- names no change request, and saying so is
        # this module's job rather than leaving by way of an exception.
        return None
    if parsed.scheme not in ("https", "http") or not hostname:
        return None
    hostname = hostname.lower()
    authority = hostname
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


def pull_request_url(project: str, number: int) -> str:
    """The pull-request URL a `project` key reads back as, on its own host.

    The inverse of the keying above, kept beside it so a caller holding only a
    key -- a channel, a stored record -- spells the URL the way this module
    will read it: bare `owner/repo` is github.com, and anything else leads with
    the authority it was keyed under, port included.
    """
    authority, _, rest = project.partition("/")
    if "/" not in rest:
        return f"https://github.com/{project}/pull/{number}"
    return f"https://{authority}/{rest}/pull/{number}"


def remote_project(remote_url: str) -> str | None:
    """The project a git remote URL names, keyed the way a change request is.

    Both spellings a forge hands out are read: `https://host/owner/repo.git`
    and the scp-like `git@host:owner/repo.git`. The key is the one
    `change_request` writes, so what a push wrote to can be compared with where
    a pull request is said to live -- which is the only reason this is here and
    not spelled again by each caller.

    A remote naming no host, or naming one by a bare path, is no project: a
    push to `/tmp/other.git` says nothing about a repository on a forge. A
    forge served on a non-default web port keys its change requests by that
    port, which a remote does not carry, so its pushes name no project here
    either and its pull requests are confirmed some other way.
    """
    remote = remote_url.strip()
    if "://" in remote:
        try:
            parsed = urlsplit(remote)
            if parsed.port is not None or parsed.scheme not in ("https", "ssh"):
                return None
        except ValueError:
            return None
        hostname, path = parsed.hostname, parsed.path
    else:
        hostname, separator, path = remote.rpartition("@")[2].partition(":")
        if not separator:
            return None
    if not hostname:
        return None
    steps = path.strip("/").removesuffix(".git").split("/")
    if len(steps) < 2 or not all(names_a_project_step(step) for step in steps):
        return None
    written = "/".join(steps).lower()
    host = hostname.lower()
    return written if host == "github.com" else f"{host}/{written}"


def _merge_request(authority: str, hostname: str, path: str) -> ChangeRequest | None:
    """Read `<project path>/-/merge_requests/<iid>`.

    A GitLab project is nested to any depth, so the project is whatever stands
    before the first marker. Segments past the number are GitLab's own views of
    the one merge request -- `/diffs`, `/commits`, `/pipelines` -- and are read
    past, as `/files` is on a pull request. A second marker among them, or a
    `pull`, would be a second change request, and is refused.

    A `pull/<number>` inside the project path is refused for the same reason a
    second marker is -- `/x/y/pull/7/-/merge_requests/1` is one change request
    to whoever reads pull requests and another to whoever reads merge requests.
    A project or group merely *named* `pull` carries no number after it and is
    the merge request it looks like.
    """
    project, _, tail = path.partition(_MERGE_REQUESTS)
    iid, _, remainder = tail.partition("/")
    number = change_request_number(iid)
    trailing = remainder.split("/")
    segments = project.strip("/").split("/")
    doubled = any(
        one == "pull" and change_request_number(following) is not None
        for one, following in zip(segments, segments[1:])
    )
    named = all(names_a_project_step(one) for one in segments)
    second = any(one in ("-", "merge_requests", "pull") for one in trailing)
    if not named or number is None or doubled or second:
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
    pasted into an API address. `https://github.com/../x/pull/1` would then
    leave as a request to `/x/issues/1/comments`, so the request the code reads
    is not the request that is sent.
    """
    return bool(segment) and segment not in (".", "..")


def change_request_number(segment: str) -> int | None:
    """Read a change-request number the way every reader of one must.

    `str.isdigit` is true of `١٢` and of `²`, and a leading zero is a number to
    a parser reading digits and not to one matching `[1-9][0-9]*`, so each of
    those spellings is a change request to one reader and not to another. `²`
    is worse than a disagreement: `int` raises on it, so a URL spelled that way
    leaves by way of an exception rather than an answer either way.

    A run of thousands of digits is that same exception from the other side:
    `int` refuses a decimal string past its conversion limit. No forge counts
    that far -- `_MOST_DIGITS` is already past a signed 64-bit counter -- so a
    longer run is not a number here either, and is refused rather than raised.
    """
    if (
        not segment.isascii()
        or not segment.isdigit()
        or segment.startswith("0")
        or len(segment) > _MOST_DIGITS
    ):
        return None
    return int(segment)


__all__ = [
    "ChangeRequest",
    "change_request",
    "change_request_number",
    "names_a_project_step",
    "pull_request_url",
    "remote_project",
]
