"""GitHub source control: git locally and a selectable GitHub API transport.

Two mechanisms, one capability. `git` handles everything that happens inside a
checkout (branching, committing, pushing) and `httpx` handles everything that
talks to github.com (pull requests, comments). Which one a method uses is an
implementation detail of that method.

The adapter is composed with the workspace provider because every operation is
keyed by `WorkspaceId` and git needs a directory. Resolving it here rather than
letting callers pass a path is what keeps the engine from ever holding one.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from urllib.parse import quote, urlparse

from engine.adapters.source_control.github.transports import (
    GitHubApiTransport,
    GitHubOAuthTransport,
    GitHubTransportError,
    normalized_authority,
)
from engine.domain.ids import WorkspaceId
from engine.ports.source_control import (
    ChangeRequest,
    CommentResult,
    Discussion,
    GitResult,
    JobLogs,
    Pipeline,
    PipelineRetry,
    PipelineStatus,
    StatusCheck,
    WorkItem,
)
from engine.ports.workspace_provider import WorkspaceProvider
from engine.runtime.change_requests import change_request, names_a_project_step, pull_request_url, remote_project
from engine.runtime.issue_links import issue_body, issue_reference
from engine.runtime.push_policy import push_spec

#: The branch prefix `GitWorktreeWorkspaceProvider` gives every workspace. It
#: is Engine's bookkeeping, not anybody's proposed change, and a remote branch
#: named after it is a leak of the internals into somebody's repository -- so
#: publishing one is refused here rather than asked for in a prompt, which is
#: the difference between a rule and a suggestion.
INTERNAL_BRANCH_PREFIX = "engine/"

#: Git's own options -- the ones before a subcommand -- that this tool will
#: pass on. An allowlist rather than a list of refusals, because the options
#: worth refusing cannot be enumerated: `-c` alone reaches `alias.*`,
#: `core.pager`, `core.sshCommand`, `diff.external`, `credential.helper` and
#: every other config key whose value git runs as a program, and the next
#: release may add another. Naming what passes is a rule that stays true.
#:
#: Everything here changes how git reads its own arguments or writes its own
#: output, and none of it runs a program or chooses a repository. `--help` is
#: absent for that reason: it hands off to a man viewer.
_PERMITTED_GLOBAL_OPTIONS = frozenset(
    {
        "-P",
        "--no-pager",
        "--no-advice",
        "--no-lazy-fetch",
        "--no-optional-locks",
        "--no-replace-objects",
        "--literal-pathspecs",
        "--glob-pathspecs",
        "--noglob-pathspecs",
        "--icase-pathspecs",
        "--version",
    }
)

#: `git push` options that consume the argument after them. Needed only so a
#: value like `--receive-pack /usr/bin/git-receive-pack` is not mistaken for a
#: refspec while working out what a push would actually create.
_PUSH_OPTIONS_TAKING_A_VALUE = frozenset(
    {"-o", "--push-option", "--receive-pack", "--exec", "--repo"}
)

#: `git push` options that push every local branch rather than a named one, so
#: the argument vector names no destination and the refs do.
_PUSH_OPTIONS_TAKING_EVERY_BRANCH = frozenset(
    {"--all", "--branches", "--mirror"}
)

#: The refspecs that mean "the branch that is checked out" rather than naming
#: one. `git push origin HEAD` creates a remote branch named after the current
#: one, which is a name only the checkout knows.
_CHECKED_OUT_REFSPECS = frozenset({"HEAD", "@"})

#: A push whose target is inferred from configuration or the checked-out ref
#: cannot be proved not to publish Engine's branch. Agents can express every
#: ordinary publish explicitly (`agent/topic` or `HEAD:agent/topic`), so the
#: adapter refuses the ambiguous spellings instead of trying to reproduce
#: git's configuration-dependent refspec resolution.
_AMBIGUOUS_PUSH_REFSPECS = frozenset({":", "+:"})
_MAX_LOG_BYTES = 8 * 1024 * 1024
_MAX_LOG_CHARACTERS = 48_000


class GitHubSourceControl:
    """Branches, commits, pushes, and pull requests against GitHub.

    Implements `engine.ports.SourceControl`.
    """

    def __init__(
        self,
        token: str | Callable[[], str | None],
        api_url: str = "https://api.github.com",
        workspace_provider: WorkspaceProvider | None = None,
        git_binary_path: str = "git",
        transport: GitHubApiTransport | None = None,
        host_aliases: Mapping[str, str] | None = None,
        resolve_addressed_threads: bool = True,
    ) -> None:
        self._resolve_addressed_threads = resolve_addressed_threads
        self._transport = transport or GitHubOAuthTransport(token, api_url)
        # Aliases explicitly name the transport they belong to. An alias of
        # another forge must never authorize posting through this transport.
        self._hosts = frozenset(
            normalized_authority(alias)
            for alias, target in (host_aliases or {}).items()
            if normalized_authority(target) == self._transport.host
        )
        self._workspace_provider = workspace_provider
        self._git_binary_path = git_binary_path

    async def run_git(
        self, workspace_id: WorkspaceId, arguments: Sequence[str],
        *, owned_pull_requests: Sequence[tuple[str, int]] = (),
    ) -> GitResult:
        """Run any git subcommand inside one workspace's checkout.

        The broker obtains approval before this method is called. The adapter
        rejects global execution overrides and ambiguous or internal push
        targets. Force pushes additionally require trusted work order ownership
        and a live check of feature branch eligibility.
        """

        arguments = tuple(str(argument) for argument in arguments)
        if not arguments:
            raise ValueError("git needs at least one argument")
        subcommand = _subcommand_index(arguments)
        if subcommand is not None and arguments[subcommand] in {"send-pack", "http-push"}:
            raise ValueError("use git push so branch ownership can be checked")
        root_path = await self._root_path(workspace_id)
        if subcommand is not None and arguments[subcommand] == "push":
            self._refuse_internal_publication(arguments[subcommand:])
            remote, branches, force = push_spec(arguments[subcommand:])
            if force:
                await self._require_owned_feature(root_path, remote, branches, owned_pull_requests)
            # Explicit refspecs and no mirror prevent local remote configuration
            # from expanding a checked push into unrelated branch rewrites.
            arguments = (*arguments[:subcommand + 1], "--no-mirror", *arguments[subcommand + 1:])
        return await self._git(root_path, arguments)

    async def create_branch(
        self, workspace_id: WorkspaceId, name: str, base_ref: str
    ) -> None:
        await self._git_checked(
            await self._root_path(workspace_id), ("checkout", "-b", name, base_ref)
        )

    async def commit_all(self, workspace_id: WorkspaceId, message: str) -> str:
        root_path = await self._root_path(workspace_id)
        await self._git_checked(root_path, ("add", "--all"))
        await self._git_checked(root_path, ("commit", "--message", message))
        return await self._git_checked(root_path, ("rev-parse", "HEAD"))

    async def publish(self, workspace_id: WorkspaceId, branch: str) -> None:
        _refuse_internal_branch(branch)
        result = await self.run_git(workspace_id, ("push", "--set-upstream", "origin", branch))
        if not result.ok:
            raise GitHubSourceControlError(result.stderr or result.stdout)

    async def request_review(
        self,
        workspace_id: WorkspaceId,
        branch: str,
        base_ref: str,
        title: str,
        body: str,
        *, issue: dict[str, object] | None = None, issue_resolution: str | None = None,
        owned_pull_requests: Sequence[tuple[str, int]] = (),
    ) -> str:
        """Open a pull request via the GitHub API and return its URL."""

        if not branch.strip():
            raise ValueError("branch must not be empty")
        if not title.strip():
            raise ValueError("title must not be empty")
        _refuse_internal_branch(branch)

        root_path = await self._root_path(workspace_id)
        owner, repo = await self._repo_coords(root_path)
        base = _base_branch(base_ref)
        if issue:
            reference = issue_reference(issue, f"{owner}/{repo}")
            qualified = f"{issue['repository']}#{issue['number']}"
            body = issue_body(body, reference, issue_resolution, qualified_reference=qualified)
            await self._issue_head(root_path, branch, base, reference, issue_resolution, qualified, owned_pull_requests=owned_pull_requests)

        response = _object(await self._api(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            json={"title": title, "body": body, "head": branch, "base": base},
        ))
        url = response.get("html_url", "")
        if not url:
            raise GitHubSourceControlError("GitHub API returned no pull-request URL")
        logging.getLogger(__name__).info(
            "GitHub pull request created url=%s author=%s transport=%s",
            url,
            _nested_string(response, "user", "login") or "unknown",
            type(self._transport).__name__,
        )
        return url

    async def _issue_head(
        self, root: str, branch: str, base: str, reference: str,
        resolution: str, qualified_reference: str = "",
        *, owned_pull_requests: Sequence[tuple[str, int]] = (),
    ) -> None:
        """Amend only this published head, refusing concurrent remote changes."""
        if branch == base:
            raise ValueError("issue publishing must not rewrite the pull request base branch")
        current = await self._git_checked(root, ("branch", "--show-current"))
        if current != branch or await self._git_checked(root, ("-c", "core.fsmonitor=false", "status", "--porcelain")):
            raise ValueError("issue publishing requires the named branch checked out with a clean workspace")
        head = await self._git_checked(root, ("rev-parse", "HEAD"))
        old = await self._git_checked(root, ("log", "-1", "--format=%B"))
        updated = issue_body(old, reference, resolution, qualified_reference=qualified_reference)
        # Keep Refs on every issue commit, including the closing head.
        if resolution == "resolves":
            updated += f"\n\nRefs {reference}"
        remote = await self._git_checked(root, ("ls-remote", "origin", f"refs/heads/{branch}"))
        if remote.split()[:1] != [head]:
            raise ValueError("push the current head before opening the issue pull request")
        if old.strip() == updated.strip():
            return
        await self._require_owned_feature(root, "origin", (branch,), owned_pull_requests)
        # Ownership and branch eligibility are checked before any local amendment.
        # This host-side metadata rewrite preserves existing credit trailers.
        # Do not execute checkout-controlled hooks while amending or publishing it.
        no_hooks = ("-c", f"core.hooksPath={os.devnull}")
        await self._git_checked(root, (*no_hooks, "-c", "core.fsmonitor=false", "-c", "commit.gpgsign=false", "commit", "--amend", "--only", "--allow-empty", "--message", updated))
        amended = await self._git_checked(root, ("rev-parse", "HEAD"))
        try:
            await self._git_checked(root, (*no_hooks, "push", "--no-mirror", f"--force-with-lease=refs/heads/{branch}:{head}",
                                           "origin", f"HEAD:refs/heads/{branch}"))
        except GitHubSourceControlError:
            # A transport error can arrive after the remote accepted the push.
            # If reconciliation fails, preserve HEAD rather than guess its state.
            remote = await self._git_checked(root, ("ls-remote", "origin", f"refs/heads/{branch}"))
            if remote.split()[:1] != [amended]:
                # The amend changes only metadata. Restore the ref without touching
                # the index/worktree, and refuse to overwrite a concurrent local move.
                await self._git_checked(root, (*no_hooks, "update-ref", f"refs/heads/{branch}", head, amended))
            raise

    async def _require_owned_feature(
        self, root: str, remote: str, branches: Sequence[str],
        owned: Sequence[tuple[str, int]],
    ) -> None:
        """Fail closed: only recorded open PRs on explicit feature namespaces."""
        denied = "force push requires a work-order-owned open PR on an unprotected agent/ or feature/ branch"
        if not owned:
            raise ValueError(denied + "; prepare issue trailers before the first push and PR creation")
        if not branches or any(not name.startswith(("agent/", "feature/")) for name in branches):
            raise ValueError(denied)
        if remote_project(remote) is None:
            remote = await self._git_checked(root, ("remote", "get-url", "--push", "--all", remote))
        if len(remote.splitlines()) != 1:
            raise ValueError(denied)
        project = remote_project(remote)
        numbers = [number for owner, number in owned if owner == project]
        if not numbers:
            raise ValueError(denied)
        owner, repo, _ = _pull_request_parts(pull_request_url(project, 1), self._hosts | {self._transport.host})
        path = f"/repos/{owner}/{repo}"
        repository = _object(await self._api("GET", path))
        default = repository.get("default_branch")
        repository_id = repository.get("id")
        if not default or repository_id is None:
            raise ValueError(denied)
        pulls = [_object(await self._api("GET", f"{path}/pulls/{number}")) for number in numbers]
        for branch in branches:
            if branch == default or any(_nested_string(pull, "base", "ref") == branch for pull in pulls):
                raise ValueError(denied)
            details = _object(await self._api("GET", f"{path}/branches/{quote(branch, safe='')}"))
            if details.get("name") != branch or details.get("protected") is not False:
                raise ValueError(denied)
            # A branch receiving other PRs is an integration target, even if
            # its name happens to use the feature namespace.
            targets = _objects(await self._api(
                "GET", f"{path}/pulls", params={"state": "open", "base": branch, "per_page": 1}
            ))
            if targets:
                raise ValueError(denied)
            if not any(
                pull.get("state") == "open"
                and _nested_string(pull, "head", "ref") == branch
                and pull.get("head", {}).get("repo", {}).get("id") == repository_id
                and pull.get("base", {}).get("repo", {}).get("id") == repository_id
                for pull in pulls
            ):
                raise ValueError(denied)

    async def can_write_repository(
        self, pr_url: str, username: str, *, user_id: int | None = None
    ) -> bool:
        """Check effective access, including team and organization grants."""
        owner, repo, _ = _pull_request_parts(pr_url, self._hosts | {self._transport.host})
        response = await self._api(
            "GET", f"/repos/{owner}/{repo}/collaborators/{quote(username, safe='')}/permission"
        )
        # GitHub maps maintain to write and triage to read, including custom
        # roles' base permissions. Unknown/missing permissions never grant access.
        if response.get("permission") not in ("write", "admin"):
            return False
        if user_id is None:
            return True
        user = response.get("user")
        return isinstance(user, dict) and type(user.get("id")) is int and user["id"] == user_id

    async def authenticated_login(self, repository_url: str) -> str:
        """Who this token posts as, so Engine can recognise its own comments.

        A GitHub app is recognisable from a comment's ``Bot`` user type, but a
        personal access token belonging to a machine user is not: asking the
        API who is calling is the only way to tell Engine's own replies apart
        from everybody else's.
        """
        login = _string(_object(await self._api("GET", "/user")), "login")
        if not login:
            raise GitHubSourceControlError("GitHub API returned no authenticated login")
        return login

    async def branch_tips(self, project: str, destinations: Sequence[str]) -> dict[str, str]:
        owner, repo, _ = _pull_request_parts(
            pull_request_url(project, 1), self._hosts | {self._transport.host}
        )
        tips: dict[str, str] = {}
        for name in dict.fromkeys(destinations):
            ref = "refs/heads/" + name
            matches = _objects(await self._api(
                "GET", f"/repos/{owner}/{repo}/git/matching-refs/heads/{quote(name, safe='')}"
            ))
            for branch in matches:
                if _string(branch, "ref") != ref:
                    continue
                sha = _nested_string(branch, "object", "sha")
                if not sha:
                    raise GitHubSourceControlError("GitHub returned an invalid branch tip")
                tips[name] = sha
        return tips

    async def add_reaction(
        self, pr_url: str, comment_id: int, content: str, *, review_comment: bool = False,
    ) -> None:
        owner, repo, _ = _pull_request_parts(pr_url, self._hosts | {self._transport.host})
        _positive_number(comment_id, "comment_id")
        if content not in ("+1", "-1", "eyes"):
            raise ValueError("unsupported reaction content")
        kind = "pulls" if review_comment else "issues"
        endpoint = f"/repos/{owner}/{repo}/{kind}/comments/{comment_id}/reactions"
        if content in ("+1", "-1"):
            login = await self.authenticated_login(pr_url)
            opposite = "-1" if content == "+1" else "+1"
            stale = []
            page = 1
            while True:
                reactions = _objects(await self._api(
                    "GET", endpoint,
                    params={"content": opposite, "per_page": 100, "page": page},
                ))
                stale.extend(
                    reaction["id"] for reaction in reactions
                    if _nested_string(reaction, "user", "login").lower() == login.lower()
                    and reaction.get("content") == opposite
                )
                if len(reactions) < 100:
                    break
                page += 1
            # Remove before adding, so a failed deletion cannot leave both verdicts.
            for reaction_id in stale:
                await self._api("DELETE", f"{endpoint}/{reaction_id}")
        await self._api("POST", endpoint, json={"content": content})

    async def add_comment(
        self,
        pr_url: str,
        comment: str,
        file: str | None = None,
        line: int | None = None,
        in_reply_to_id: int | None = None,
        *, thread_id: str | None = None, resolve: bool = False, commit_sha: str | None = None,
    ) -> CommentResult:
        """Add a general or inline pull-request comment via the GitHub API."""

        if not pr_url.strip():
            raise ValueError("pr_url must not be empty")
        if not comment.strip():
            raise ValueError("comment must not be empty")
        if file is not None and not file.strip():
            raise ValueError("file must not be empty")
        if (file is None) != (line is None):
            raise ValueError("file and line must be provided together")
        if line is not None and (
            not isinstance(line, int) or isinstance(line, bool) or line < 1
        ):
            raise ValueError("line must be a positive integer")

        if in_reply_to_id is not None:
            _positive_number(in_reply_to_id, "in_reply_to_id")
            if file is not None or line is not None:
                raise ValueError("in_reply_to_id cannot be combined with file or line")

        owner, repo, number = _pull_request_parts(pr_url, self._hosts | {self._transport.host})

        if resolve and (not thread_id or not commit_sha or not re.fullmatch(r"[0-9a-fA-F]{7,40}", commit_sha)):
            raise ValueError("resolving a reply requires thread_id and commit_sha")
        if thread_id:
            _positive_number(in_reply_to_id, "in_reply_to_id")
            thread, replies = await self._review_thread_by_id(pr_url, thread_id, include_replies=resolve)
            if thread.comment_id != in_reply_to_id:
                raise ValueError("thread_id does not match the review comment")
        if resolve:
            comment = f"Addressed in {commit_sha}: {comment}"

        if in_reply_to_id is not None:
            response = None
            if resolve:
                # A reply may succeed while resolution fails. Reuse our exact
                # reply on retry, including after a process restart.
                matching = [entry for entry in replies if entry["body"] == comment]
                if matching:
                    login = await self.authenticated_login(pr_url)
                    match = next((entry for entry in matching
                                  if (entry.get("author") or {}).get("login", "").lower() == login.lower()), None)
                    if match:
                        response = {"id": match["databaseId"], "html_url": match["url"]}
            if response is None:
                response = await self._api(
                    "POST",
                    f"/repos/{owner}/{repo}/pulls/{number}/comments/{in_reply_to_id}/replies",
                    json={"body": comment},
                )
            if resolve:
                try:
                    await self._resolve_validated_thread(thread)
                except Exception as error:
                    raise GitHubSourceControlError(
                        f"Reply posted at {response['html_url']}, but thread resolution failed; "
                        f"repeat the same add_comment to retry without duplicating it: {error}"
                    ) from error
            return CommentResult(response["id"], response["html_url"])

        if file is None:
            response = await self._api(
                "POST",
                f"/repos/{owner}/{repo}/issues/{number}/comments",
                json={"body": comment},
            )
            return CommentResult(response["id"], response["html_url"])

        # Inline comment: resolve the PR head SHA first.
        pr_data = await self._api("GET", f"/repos/{owner}/{repo}/pulls/{number}")
        head_sha = pr_data.get("head", {}).get("sha", "")
        if not head_sha:
            raise GitHubSourceControlError(
                "GitHub API returned an empty pull-request head SHA"
            )
        response = await self._api(
            "POST",
            f"/repos/{owner}/{repo}/pulls/{number}/comments",
            json={
                "body": comment,
                "commit_id": head_sha,
                "path": file,
                "line": line,
                "side": "RIGHT",
            },
        )
        return CommentResult(response["id"], response["html_url"])

    async def _graphql(self, query: str, **variables: object) -> dict:
        response = _object(await self._api("POST", "/graphql", json={"query": query, "variables": variables}))
        if response.get("errors"):
            raise GitHubSourceControlError(f"GitHub GraphQL failed: {response['errors']}")
        if not isinstance(response.get("data"), dict):
            raise GitHubSourceControlError("GitHub GraphQL returned no data")
        return response["data"]

    async def _review_threads(self, pr_url: str, *, comment_id: int | None = None) -> tuple[Discussion, ...]:
        owner, repo, number = _pull_request_parts(pr_url, self._hosts | {self._transport.host})
        query = """query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
          repository(owner: $owner, name: $repo) { pullRequest(number: $number) {
            reviewThreads(first: 100, after: $cursor) {
              nodes { id isResolved comments(first: 1) {
                nodes { databaseId body url author { login } path line }
              } }
              pageInfo { hasNextPage endCursor }
            }
          } }
        }"""
        threads = []
        cursor = None
        while True:
            data = await self._graphql(query, owner=owner, repo=repo, number=int(number), cursor=cursor)
            connection = data["repository"]["pullRequest"]["reviewThreads"]
            for thread in connection["nodes"]:
                root = thread["comments"]["nodes"][0]
                threads.append(Discussion(
                    author=(root.get("author") or {}).get("login", ""),
                    body=root["body"], url=root["url"], path=root.get("path"), line=root.get("line"),
                    comment_id=root["databaseId"], thread_id=thread["id"], is_resolved=thread["isResolved"],
                ))
                if comment_id == root["databaseId"]:
                    return (threads[-1],)
            page = connection["pageInfo"]
            if not page["hasNextPage"]:
                return tuple(threads)
            following = page["endCursor"]
            if not following or following == cursor:
                raise GitHubSourceControlError("GitHub returned an invalid review thread cursor")
            cursor = following

    async def review_thread(self, pr_url: str, comment_id: int) -> Discussion:
        _positive_number(comment_id, "comment_id")
        for thread in await self._review_threads(pr_url, comment_id=comment_id):
            if thread.comment_id == comment_id:
                return thread
        raise ValueError("review comment is not a root thread on this pull request")

    async def resolve_review_thread(self, pr_url: str, thread_id: str) -> bool:
        thread, _ = await self._review_thread_by_id(pr_url, thread_id)
        return await self._resolve_validated_thread(thread)

    async def _review_thread_by_id(
        self, pr_url: str, thread_id: str, *, include_replies: bool = False,
    ) -> tuple[Discussion, list[dict]]:
        """Validate one thread and optionally fetch only its replies for retries."""
        owner, repo, number = _pull_request_parts(pr_url, self._hosts | {self._transport.host})
        query = """query($thread: ID!, $cursor: String, $count: Int!) {
          node(id: $thread) { ... on PullRequestReviewThread {
            id isResolved pullRequest { number repository { nameWithOwner } }
            comments(first: $count, after: $cursor) {
              nodes { databaseId body url author { login } path line }
              pageInfo { hasNextPage endCursor }
            }
          } }
        }"""
        cursor = None
        discussion = None
        replies = []
        while True:
            data = await self._graphql(
                query, thread=thread_id, cursor=cursor, count=100 if include_replies else 1,
            )
            node = data.get("node")
            pull = (node or {}).get("pullRequest", {})
            if (not node or node.get("id") != thread_id
                    or pull.get("number") != int(number)
                    or pull.get("repository", {}).get("nameWithOwner", "").lower() != f"{owner}/{repo}".lower()):
                raise ValueError("review thread does not belong to this pull request")
            connection = node["comments"]
            comments = connection["nodes"]
            if discussion is None:
                if not comments:
                    raise ValueError("review thread has no root comment")
                root = comments[0]
                discussion = Discussion(
                    author=(root.get("author") or {}).get("login", ""),
                    body=root["body"], url=root["url"], path=root.get("path"), line=root.get("line"),
                    comment_id=root["databaseId"], thread_id=node["id"], is_resolved=node["isResolved"],
                )
                replies.extend(comments[1:])
            else:
                replies.extend(comments)
            page = connection["pageInfo"]
            if not include_replies or not page["hasNextPage"]:
                return discussion, replies
            following = page["endCursor"]
            if not following or following == cursor:
                raise GitHubSourceControlError("GitHub returned an invalid review comment cursor")
            cursor = following

    async def _resolve_validated_thread(self, thread: Discussion) -> bool:
        """Resolve a thread whose membership the caller has already checked."""
        if not self._resolve_addressed_threads:
            return False
        if thread.is_resolved:
            return True
        data = await self._graphql("""mutation($thread: ID!) {
          resolveReviewThread(input: {threadId: $thread}) { thread { id isResolved } }
        }""", thread=thread.thread_id)
        if data["resolveReviewThread"]["thread"]["isResolved"] is not True:
            raise GitHubSourceControlError("GitHub did not resolve the review thread")
        return True

    async def view_change_request(
        self, workspace_id: WorkspaceId, number: int
    ) -> ChangeRequest:
        owner, repo = await self._workspace_repo(workspace_id)
        number = _positive_number(number, "number")
        pull = _object(await self._api("GET", f"/repos/{owner}/{repo}/pulls/{number}"))
        reviews = await self._paginated_objects(f"/repos/{owner}/{repo}/pulls/{number}/reviews")
        issue_comments = await self._paginated_objects(
            f"/repos/{owner}/{repo}/issues/{number}/comments"
        )
        inline_comments = await self._paginated_objects(
            f"/repos/{owner}/{repo}/pulls/{number}/comments"
        )
        threads = {thread.comment_id: thread for thread in await self._review_threads(_string(pull, "html_url"))} if inline_comments else {}
        discussions = []
        for comment in inline_comments:
            discussion = _discussion(comment)
            thread = threads.get(comment.get("in_reply_to_id") or comment.get("id"))
            if thread:
                discussion = replace(discussion, thread_id=thread.thread_id, is_resolved=thread.is_resolved)
            discussions.append(discussion)
        return ChangeRequest(
            number=number,
            title=_string(pull, "title"),
            state=_string(pull, "state"),
            body=_string(pull, "body"),
            author=_nested_string(pull, "user", "login"),
            url=_string(pull, "html_url"),
            head_is_same_repository=(
                isinstance(pull.get("head"), dict)
                and isinstance(pull["head"].get("repo"), dict)
                and isinstance(pull.get("base"), dict)
                and isinstance(pull["base"].get("repo"), dict)
                and pull["head"]["repo"].get("id") is not None
                and pull["head"]["repo"].get("id") == pull["base"]["repo"].get("id")
            ),
            head_ref=_nested_string(pull, "head", "ref"),
            head_sha=_nested_string(pull, "head", "sha"),
            base_ref=_nested_string(pull, "base", "ref"),
            reviews=tuple(_discussion(review) for review in reviews),
            comments=tuple(_discussion(comment) for comment in issue_comments) + tuple(discussions),
        )

    async def list_work_items(
        self,
        workspace_id: WorkspaceId,
        state: str = "open",
        labels: Sequence[str] = (),
        limit: int = 30,
    ) -> tuple[WorkItem, ...]:
        owner, repo = await self._workspace_repo(workspace_id)
        state = _work_item_state(state)
        limit = _limit(limit)
        data = await self._paginated_objects(
            f"/repos/{owner}/{repo}/issues",
            {"state": state, "labels": ",".join(labels)},
        )
        return tuple(
            _work_item(issue)
            for issue in data
            if not isinstance(issue.get("pull_request"), dict)
        )[:limit]

    async def view_work_item(self, workspace_id: WorkspaceId, number: int) -> WorkItem:
        owner, repo = await self._workspace_repo(workspace_id)
        number = _positive_number(number, "number")
        issue = _object(await self._api("GET", f"/repos/{owner}/{repo}/issues/{number}"))
        comments = await self._paginated_objects(f"/repos/{owner}/{repo}/issues/{number}/comments")
        return _work_item(issue, tuple(_discussion(comment) for comment in comments))

    async def list_pipeline_status(
        self,
        workspace_id: WorkspaceId,
        *,
        ref: str | None = None,
        change_request_number: int | None = None,
    ) -> PipelineStatus:
        owner, repo = await self._workspace_repo(workspace_id)
        requirements = None
        if change_request_number is not None:
            if ref is not None:
                raise ValueError("provide exactly one of ref or change_request_number")
            number = _positive_number(change_request_number, "change_request_number")
            pull = _object(await self._api("GET", f"/repos/{owner}/{repo}/pulls/{number}"))
            ref = _nested_string(pull, "head", "sha")
            base = _nested_string(pull, "base", "ref")
            if not ref or not base:
                raise GitHubSourceControlError("GitHub returned an empty PR head or base")
            requirements = await self._required_checks(owner, repo, base)
        else:
            ref = await self._status_ref(owner, repo, ref, change_request_number)
        checks = await self._paginated_field(f"/repos/{owner}/{repo}/commits/{ref}/check-runs", "check_runs")
        runs = await self._paginated_field(f"/repos/{owner}/{repo}/actions/runs", "workflow_runs", {"head_sha": ref})
        # Combined status returns the latest value for each legacy context.
        statuses = await self._paginated_field(
            f"/repos/{owner}/{repo}/commits/{ref}/status", "statuses"
        )
        legacy = tuple(
            StatusCheck(_string(item, "context"), _string(item, "state"), None,
                        _string(item, "target_url"))
            for item in statuses
        )
        required = None
        if requirements is not None:
            required = []
            for name, app_id in requirements:
                matches = [
                    StatusCheck(name, _string(check, "status"),
                                _optional_string(check, "conclusion"),
                                _string(check, "details_url"))
                    for check in checks
                    if check.get("name") == name and (
                        app_id is None or _object(check.get("app", {})).get("id") == app_id
                    )
                ]
                if app_id is None:
                    matches.extend(item for item in legacy if item.name == name)
                required.extend(matches or [StatusCheck(name, "pending", None, "")])
        return PipelineStatus(
            ref=ref,
            required_checks=tuple(required) if required is not None else None,
            checks=legacy + tuple(
                StatusCheck(
                    name=_string(check, "name"),
                    status=_string(check, "status"),
                    conclusion=_optional_string(check, "conclusion"),
                    details_url=_string(check, "details_url"),
                )
                for check in checks
            ),
            pipelines=tuple(
                Pipeline(
                    pipeline_id=_positive_number(run.get("id"), "workflow run id"),
                    name=_string(run, "name"),
                    status=_string(run, "status"),
                    conclusion=_optional_string(run, "conclusion"),
                    url=_string(run, "html_url"),
                )
                for run in runs
            ),
        )

    async def _required_checks(
        self, owner: str, repo: str, base: str,
    ) -> tuple[tuple[str, int | None], ...]:
        """Combine classic branch protection and all active branch rulesets.

        Errors propagate: an unreadable policy is not an empty policy.
        Keep app identities so an unrelated check cannot satisfy a gate.
        """
        branch = quote(base, safe="")
        data = _object(await self._api("GET", f"/repos/{owner}/{repo}/branches/{branch}"))
        protection = data.get("protection")
        if not isinstance(protection, dict) and data.get("protected") is not False:
            raise GitHubSourceControlError("GitHub did not report branch protection")
        policy = _object((protection or {}).get("required_status_checks") or {})
        requirements: set[tuple[str, int | None]] = set()
        checks = _objects(policy.get("checks", []))
        for check in checks:
            app_id = check.get("app_id")
            requirements.add((_string(check, "context"), app_id if app_id != -1 else None))
        for context in policy.get("contexts", []):
            if not any(name == context for name, _ in requirements):
                requirements.add((context, None))
        rules = await self._paginated_objects(f"/repos/{owner}/{repo}/rules/branches/{branch}")
        for rule in rules:
            if rule.get("type") == "workflows":
                logging.getLogger(__name__).warning(
                    "Unsupported required workflows ruleset for %s/%s:%s; "
                    "skipping workflow verification and failing open for this rule",
                    owner, repo, base,
                )
            if rule.get("type") == "required_status_checks":
                for check in _objects(_object(rule.get("parameters", {})).get("required_status_checks", [])):
                    requirements.add((_string(check, "context"), check.get("integration_id")))
        return tuple(sorted(requirements, key=lambda item: (item[0], item[1] or -1)))

    async def get_job_logs(
        self, workspace_id: WorkspaceId, pipeline_id: int, job_id: int | None = None
    ) -> JobLogs:
        owner, repo = await self._workspace_repo(workspace_id)
        pipeline_id = _positive_number(pipeline_id, "pipeline_id")
        if job_id is None:
            jobs = _object(
                await self._api("GET", f"/repos/{owner}/{repo}/actions/runs/{pipeline_id}/jobs")
            )
            candidates = _objects(jobs.get("jobs", []))
            failed = [job for job in candidates if job.get("conclusion") == "failure"]
            completed = [job for job in candidates if job.get("status") == "completed"]
            if not (failed or completed):
                raise GitHubSourceControlError(
                    "GitHub returned no completed jobs for workflow run"
                )
            job_id = _positive_number((failed or completed)[0].get("id"), "job_id")
        else:
            job_id = _positive_number(job_id, "job_id")
        content = await self._download(f"/repos/{owner}/{repo}/actions/jobs/{job_id}/logs")
        text = _log_text(content)
        truncated = len(text) > _MAX_LOG_CHARACTERS
        return JobLogs(
            pipeline_id=pipeline_id,
            job_id=job_id,
            text=text[-_MAX_LOG_CHARACTERS:] if truncated else text,
            truncated=truncated,
        )

    async def retry_pipeline(
        self, workspace_id: WorkspaceId, pipeline_id: int, job_id: int | None = None
    ) -> PipelineRetry:
        owner, repo = await self._workspace_repo(workspace_id)
        pipeline_id = _positive_number(pipeline_id, "pipeline_id")
        if job_id is None:
            await self._api("POST", f"/repos/{owner}/{repo}/actions/runs/{pipeline_id}/rerun")
            return PipelineRetry(pipeline_id=pipeline_id)
        job_id = _positive_number(job_id, "job_id")
        await self._api("POST", f"/repos/{owner}/{repo}/actions/jobs/{job_id}/rerun")
        return PipelineRetry(pipeline_id=pipeline_id, job_id=job_id)

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    async def _root_path(self, workspace_id: WorkspaceId) -> str:
        if self._workspace_provider is None:
            raise GitHubSourceControlError(
                "this source control was composed without a workspace provider, "
                "so it cannot find the checkout to work in"
            )
        return await self._workspace_provider.root_path(workspace_id)

    async def _workspace_repo(self, workspace_id: WorkspaceId) -> tuple[str, str]:
        return await self._repo_coords(await self._root_path(workspace_id))

    async def _status_ref(
        self, owner: str, repo: str, ref: str | None, change_request_number: int | None
    ) -> str:
        if (ref is None) == (change_request_number is None):
            raise ValueError("provide exactly one of ref or change_request_number")
        if ref is not None:
            if not ref.strip():
                raise ValueError("ref must not be empty")
            return ref
        number = _positive_number(change_request_number, "change_request_number")
        pull = _object(await self._api("GET", f"/repos/{owner}/{repo}/pulls/{number}"))
        sha = _nested_string(pull, "head", "sha")
        if not sha:
            raise GitHubSourceControlError("GitHub API returned an empty pull-request head SHA")
        return sha

    def _refuse_internal_publication(self, arguments: Sequence[str]) -> None:
        """Stop a push before it puts an Engine branch on somebody's remote."""

        destinations = _push_destinations(arguments)
        for destination in destinations:
            _refuse_internal_branch(destination)

    async def _git(self, root_path: str, arguments: Sequence[str]) -> GitResult:
        try:
            process = await asyncio.create_subprocess_exec(
                self._git_binary_path,
                "-C",
                root_path,
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._git_environment(),
            )
        except OSError as error:
            raise GitHubSourceControlError(
                f"could not start {self._git_binary_path}: {error}"
            ) from error
        stdout, stderr = await process.communicate()
        return GitResult(
            exit_code=process.returncode or 0,
            stdout=stdout.decode(errors="replace").strip(),
            stderr=stderr.decode(errors="replace").strip(),
        )

    async def _git_checked(self, root_path: str, arguments: Sequence[str]) -> str:
        result = await self._git(root_path, arguments)
        if not result.ok:
            detail = result.stderr or result.stdout or "unknown error"
            raise GitHubSourceControlError(
                f"git {arguments[0]} failed: {detail}"
            )
        return result.stdout

    def _git_environment(self) -> Mapping[str, str]:
        """The host environment without forge bearer tokens.

        Git authentication belongs to the configured credential helper. A git
        subprocess does not need the token used for the GitHub API, and git can
        invoke helpers, hooks and aliases, so putting that token in its
        environment turns any such program into a credential reader.
        """

        return {
            name: value
            for name, value in os.environ.items()
            if name
            not in {
                "GH_TOKEN",
                "GITHUB_TOKEN",
                "GH_ENTERPRISE_TOKEN",
                "GITHUB_ENTERPRISE_TOKEN",
            }
        }

    async def _api(self, method: str, path: str, **kwargs: object) -> object:
        """Make one GitHub request, preserving one adapter-level error shape."""
        try:
            return await self._transport.request(method, path, **kwargs)
        except GitHubTransportError as error:
            raise GitHubSourceControlError(str(error)) from error

    async def _paginated_objects(self, path: str, params: Mapping[str, object] | None = None) -> tuple[dict, ...]:
        """Read every GitHub list page rather than silently accepting page one."""

        items: list[dict] = []
        page = 1
        while True:
            current = _objects(
                await self._api("GET", path, params={"per_page": 100, "page": page, **(params or {})})
            )
            items.extend(current)
            if len(current) < 100:
                return tuple(items)
            page += 1

    async def _paginated_field(self, path: str, field: str, params: Mapping[str, object] | None = None) -> tuple[dict, ...]:
        items: list[dict] = []
        page = 1
        while True:
            data = _object(await self._api("GET", path, params={"per_page": 100, "page": page, **(params or {})}))
            current = _objects(data.get(field, []))
            items.extend(current)
            if len(items) >= data.get("total_count", 0) or len(current) < 100:
                return tuple(items)
            page += 1

    async def _download(self, path: str) -> bytes:
        try:
            content = await self._transport.download(path)
        except GitHubTransportError as error:
            raise GitHubSourceControlError(str(error)) from error
        if len(content) > _MAX_LOG_BYTES:
            raise GitHubSourceControlError("GitHub job logs exceed the download limit")
        return content

    async def _repo_coords(self, root_path: str) -> tuple[str, str]:
        """The `owner/repo` pair from the workspace's `origin` remote URL."""
        result = await self._git_checked(
            root_path, ("remote", "get-url", "origin")
        )
        return _parse_repo_coords(result)


class GitHubSourceControlError(RuntimeError):
    """The GitHub API could not perform a source-control operation."""


class InternalBranchPublicationError(GitHubSourceControlError):
    """Something tried to publish Engine's own bookkeeping branch."""

    def __init__(self, branch: str) -> None:
        super().__init__(
            f"{branch} is an internal Engine branch and must not be published\n"
            f"hint: create a descriptive branch such as agent/<description> from "
            f"the intended base, apply only the commits meant for review, and "
            f"push that instead"
        )
        self.branch = branch


class UnsafePushSpecificationError(InternalBranchPublicationError):
    """A push leaves its destination to git configuration or bulk expansion."""

    def __init__(self, refspec: str) -> None:
        GitHubSourceControlError.__init__(
            self,
            f"push target {refspec!r} is not explicit enough to prove that it "
            "excludes Engine's internal branch; name a concrete destination "
            "such as agent/<description> or HEAD:agent/<description>",
        )
        self.branch = refspec


class GitOutsideWorkspaceError(GitHubSourceControlError):
    """A git command tried to point itself at a different repository."""

    def __init__(self, option: str) -> None:
        super().__init__(
            f"{option} would run git somewhere other than this workspace, which "
            f"is the one thing this tool does not do"
        )
        self.option = option


class GitGlobalOptionError(GitOutsideWorkspaceError):
    """A global option could change what executable git runs."""

    def __init__(self, option: str) -> None:
        GitHubSourceControlError.__init__(
            self,
            f"git global option {option} is not available through git_subcommand; "
            "pass an ordinary git subcommand and its arguments instead",
        )
        self.option = option


def _refuse_internal_branch(branch: str) -> None:
    if _branch_name(branch).startswith(INTERNAL_BRANCH_PREFIX):
        raise InternalBranchPublicationError(branch)


def _branch_name(ref: str) -> str:
    """The branch a ref names, with the decoration git allows around one."""
    return ref.lstrip("+").removeprefix("refs/heads/")


def _subcommand_index(arguments: Sequence[str]) -> int | None:
    """Where the subcommand sits, rejecting executable-selecting options.

    Git's global option surface is security-sensitive: `-c alias.x=!sh` and
    `--exec-path` both select programs before a subcommand begins. Permit only
    value-free presentation/pathspec switches whose meaning is closed here.
    """
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if not argument.startswith("-"):
            return index
        option = argument.partition("=")[0]
        if option not in _PERMITTED_GLOBAL_OPTIONS:
            raise GitGlobalOptionError(option)
        index += 1
    return None


def _push_destinations(arguments: Sequence[str]) -> tuple[str, ...]:
    """The branches a `git push` argument vector would write to.

    Only explicit, concrete destinations pass. Git otherwise consults the
    checked-out branch and `remote.*.push`, while bulk and wildcard forms can
    publish refs absent from argv. Reimplementing that resolver incompletely is
    exactly how an internal branch escaped the original guard.
    """
    positional: list[str] = []
    skip_next = False
    repository_from_option = False
    for argument in arguments[1:]:
        if skip_next:
            skip_next = False
            continue
        if argument.startswith("-"):
            option = argument.partition("=")[0]
            if option in _PUSH_OPTIONS_TAKING_EVERY_BRANCH:
                raise UnsafePushSpecificationError(option)
            skip_next = "=" not in argument and option in _PUSH_OPTIONS_TAKING_A_VALUE
            repository_from_option = repository_from_option or option == "--repo"
            continue
        positional.append(argument)

    # Ordinarily the first positional is the remote. `--repo=<remote>` supplies
    # it as an option instead, making every positional a refspec.
    refspecs = positional if repository_from_option else positional[1:]
    if not refspecs:
        # `--tags` does not publish a branch. Every other refspec-free push is
        # configuration-dependent and therefore not provably safe.
        if any(argument.partition("=")[0] == "--tags" for argument in arguments[1:]):
            return ()
        raise UnsafePushSpecificationError("implicit push refspec")

    destinations: list[str] = []
    for refspec in refspecs:
        undecorated = refspec.lstrip("+")
        if undecorated in _AMBIGUOUS_PUSH_REFSPECS or "*" in undecorated:
            raise UnsafePushSpecificationError(refspec)
        source, separator, destination = undecorated.partition(":")
        if not separator:
            if source in _CHECKED_OUT_REFSPECS:
                raise UnsafePushSpecificationError(source)
            destination = source
        elif not destination:
            raise UnsafePushSpecificationError(refspec)
        destinations.append(_branch_name(destination))
    return tuple(destinations)


def _base_branch(base_ref: str) -> str:
    """The branch `base_ref` names, as GitHub wants it written.

    Only `origin/` comes off, and only as a prefix: a base of `release/2.0` is
    a branch whose name has a slash in it, and stripping up to the first one
    would quietly propose against `2.0` instead.
    """
    return base_ref.removeprefix("origin/")


def _pull_request_parts(
    pr_url: str, hosts: frozenset[str] = frozenset({"github.com"})
) -> tuple[str, str, str]:
    """Read `owner`, `repo` and the number out of a pull-request URL.

    Read once, by the same reader CICheck and the recorders use, so a URL they
    bind the run to is a URL this can send. A second parse here is what let
    `.../pull/42/files` -- the tab a reviewer is looking at when it copies the
    address -- pass as the run's pull request everywhere else and then fail on
    the step that posts findings.
    """
    found = change_request(pr_url)
    if found is None or found.kind != "pull" or normalized_authority(pr_url) not in hosts:
        raise ValueError("pr_url must be a GitHub pull-request URL")
    owner, repo = found.path.split("/")
    if not _is_repository_name(owner) or not _is_repository_name(repo):
        raise ValueError("pr_url must be a GitHub pull-request URL")
    return owner, repo, str(found.number)


def _is_repository_name(segment: str) -> bool:
    """Whether `segment` is a name GitHub writes, and this may paste into a path.

    An owner and repository go into an API address by interpolation --
    `/repos/{owner}/{repo}/issues/...` -- and `httpx` resolves dot segments in
    what it is handed, so `..` would send the request somewhere other than
    where the line building it says. Held to the characters GitHub allows in a
    login or a repository, since a name that could not exist is never one this
    should be asking the API about.
    """
    return names_a_project_step(segment) and all(
        character.isascii() and (character.isalnum() or character in "-._")
        for character in segment
    )


def _parse_repo_coords(remote_url: str) -> tuple[str, str]:
    """Extract `(owner, repo)` from an HTTPS or SSH remote URL.

    Handles the two common spellings:
      https://github.com/owner/repo.git
      git@github.com:owner/repo.git

    Held to the same names a pull-request URL is, since these two go into an
    API address by interpolation as well.
    """
    remote_url = remote_url.strip()
    # SSH shorthand: git@github.com:owner/repo.git
    if remote_url.startswith("git@"):
        path = remote_url.split(":", 1)[-1]
    else:
        parsed = urlparse(remote_url)
        path = parsed.path
    path = path.strip("/").removesuffix(".git")
    parts = path.split("/")
    if len(parts) < 2 or not all(_is_repository_name(one) for one in parts[:2]):
        raise GitHubSourceControlError(
            f"cannot determine owner/repo from remote URL: {remote_url!r}"
        )
    return parts[0], parts[1]


def _object(value: object) -> dict:
    if not isinstance(value, dict):
        raise GitHubSourceControlError("GitHub API returned an unexpected response")
    return value


def _objects(value: object) -> tuple[dict, ...]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise GitHubSourceControlError("GitHub API returned an unexpected list response")
    return tuple(value)


def _string(value: dict, name: str) -> str:
    item = value.get(name, "")
    return item if isinstance(item, str) else ""


def _optional_string(value: dict, name: str) -> str | None:
    item = value.get(name)
    return item if isinstance(item, str) else None


def _nested_string(value: dict, outer: str, inner: str) -> str:
    nested = value.get(outer)
    return _string(nested, inner) if isinstance(nested, dict) else ""


def _discussion(value: dict) -> Discussion:
    line = value.get("line")
    return Discussion(
        comment_id=value.get("id"),
        author=_nested_string(value, "user", "login"),
        body=_string(value, "body"),
        url=_string(value, "html_url"),
        path=_optional_string(value, "path"),
        line=line if isinstance(line, int) and not isinstance(line, bool) else None,
    )


def _work_item(value: dict, comments: tuple[Discussion, ...] = ()) -> WorkItem:
    labels = value.get("labels", [])
    names = tuple(
        _string(label, "name") for label in labels if isinstance(label, dict) and _string(label, "name")
    )
    return WorkItem(
        number=_positive_number(value.get("number"), "issue number"),
        title=_string(value, "title"),
        state=_string(value, "state"),
        body=_string(value, "body"),
        author=_nested_string(value, "user", "login"),
        url=_string(value, "html_url"),
        labels=names,
        comments=comments,
    )


def _positive_number(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _limit(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 100:
        raise ValueError("limit must be an integer from 1 to 100")
    return value


def _work_item_state(value: object) -> str:
    if value not in {"open", "closed", "all"}:
        raise ValueError("state must be open, closed, or all")
    return str(value)


def _log_text(content: bytes) -> str:
    if content.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                return "\n".join(
                    archive.read(name).decode(errors="replace")
                    for name in archive.namelist()
                    if not name.endswith("/")
                )
        except (OSError, zipfile.BadZipFile) as error:
            raise GitHubSourceControlError(f"could not read GitHub job log archive: {error}") from error
    return content.decode(errors="replace")


__all__ = [
    "INTERNAL_BRANCH_PREFIX",
    "GitGlobalOptionError",
    "GitHubSourceControl",
    "GitHubSourceControlError",
    "GitOutsideWorkspaceError",
    "InternalBranchPublicationError",
    "UnsafePushSpecificationError",
]
