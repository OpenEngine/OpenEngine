"""GitLab implementation of the provider-neutral source-control port."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Sequence
from urllib.parse import quote, urlparse

from engine.adapters.source_control.gitlab.transports import GitLabOAuthTransport, GitLabTransportError
from engine.domain.ids import WorkspaceId
from engine.ports.source_control import ChangeRequest, CommentResult, Discussion, GitResult, JobLogs, Pipeline, PipelineRetry, PipelineStatus, StatusCheck, WorkItem
from engine.ports.workspace_provider import WorkspaceProvider

_MAX_LOG_CHARACTERS = 48_000


class GitLabSourceControlError(RuntimeError):
    pass


class GitLabSourceControl:
    """Publishes git changes and manages merge requests through GitLab's API."""

    def __init__(self, token: str | Callable[[], str | None], origin: str | Callable[[], str] = "https://gitlab.com", workspace_provider: WorkspaceProvider | None = None, transport: GitLabOAuthTransport | None = None) -> None:
        self._transport = transport or GitLabOAuthTransport(token, origin)
        self._workspace_provider = workspace_provider

    async def run_git(self, workspace_id: WorkspaceId, arguments: Sequence[str]) -> GitResult:
        arguments = tuple(str(argument) for argument in arguments)
        if not arguments:
            raise ValueError("git needs at least one argument")
        if arguments[0].startswith("-"):
            raise ValueError("git global options are not permitted")
        if arguments[0] == "push":
            for argument in arguments[1:]:
                destination = argument.split(":")[-1].removeprefix("refs/heads/")
                if destination.startswith("engine/"):
                    raise ValueError("branch must be a non-internal branch")
        return await self._git(await self._root(workspace_id), arguments)

    async def create_branch(self, workspace_id: WorkspaceId, name: str, base_ref: str) -> None:
        await self._checked(workspace_id, ("checkout", "-b", name, base_ref))

    async def commit_all(self, workspace_id: WorkspaceId, message: str) -> str:
        await self._checked(workspace_id, ("add", "--all"))
        await self._checked(workspace_id, ("commit", "--message", message))
        return await self._checked(workspace_id, ("rev-parse", "HEAD"))

    async def publish(self, workspace_id: WorkspaceId, branch: str) -> None:
        self._public(branch)
        await self._checked(workspace_id, ("push", "--set-upstream", "origin", branch))

    async def request_review(self, workspace_id: WorkspaceId, branch: str, base_ref: str, title: str, body: str) -> str:
        self._public(branch)
        project = await self._project(workspace_id)
        result = await self._api("POST", f"/projects/{project}/merge_requests", json={"source_branch": branch, "target_branch": base_ref, "title": title, "description": body})
        url = self._str(result, "web_url")
        if not url: raise GitLabSourceControlError("GitLab returned no merge-request URL")
        return url

    async def can_write_repository(self, pr_url: str, username: str) -> bool:
        raise NotImplementedError("GitLab repository permission checks are not supported")

    async def add_comment(self, pr_url: str, comment: str, file: str | None = None, line: int | None = None, in_reply_to_id: int | None = None) -> CommentResult:
        if in_reply_to_id is not None:
            raise NotImplementedError("GitLab comment replies are not supported")
        project, iid = self._mr_url(pr_url)
        if not comment.strip():
            raise ValueError("comment must not be empty")
        if (file is None) != (line is None):
            raise ValueError("file and line must be provided together")
        if file is None:
            note = await self._api("POST", f"/projects/{project}/merge_requests/{iid}/notes", json={"body": comment})
            return CommentResult(note["id"], f"{pr_url}#note_{note['id']}")
        if not file.strip() or not isinstance(line, int) or isinstance(line, bool) or line < 1:
            raise ValueError("file must be non-empty and line must be positive")
        changes = await self._api("GET", f"/projects/{project}/merge_requests/{iid}/changes")
        refs = changes.get("diff_refs")
        if not isinstance(refs, dict):
            raise GitLabSourceControlError("GitLab returned no merge-request diff references")
        base_sha, start_sha, head_sha = (self._str(refs, key) for key in ("base_sha", "start_sha", "head_sha"))
        if not all((base_sha, start_sha, head_sha)):
            raise GitLabSourceControlError("GitLab returned incomplete merge-request diff references")
        discussion = await self._api(
            "POST", f"/projects/{project}/merge_requests/{iid}/discussions",
            json={"body": comment, "position": {"position_type": "text", "base_sha": base_sha, "start_sha": start_sha, "head_sha": head_sha, "new_path": file, "new_line": line}},
        )

        note = discussion["notes"][0]
        return CommentResult(note["id"], f"{pr_url}#note_{note['id']}")

    async def view_change_request(self, workspace_id: WorkspaceId, number: int) -> ChangeRequest:
        project = await self._project(workspace_id); mr = await self._api("GET", f"/projects/{project}/merge_requests/{number}")
        notes = await self._list(f"/projects/{project}/merge_requests/{number}/notes")
        return ChangeRequest(number=number, title=self._str(mr,"title"), state=self._str(mr,"state"), body=self._str(mr,"description"), author=self._nested(mr,"author","username"), url=self._str(mr,"web_url"), head_ref=self._str(mr,"source_branch"), head_sha=self._str(mr,"sha"), base_ref=self._str(mr,"target_branch"), comments=tuple(self._discussion(note) for note in notes))

    async def list_work_items(self, workspace_id: WorkspaceId, state: str = "open", labels: Sequence[str] = (), limit: int = 30) -> tuple[WorkItem, ...]:
        project = await self._project(workspace_id); issues = await self._list(f"/projects/{project}/issues", {"state": state, "labels": ",".join(labels), "per_page": min(limit,100)})
        return tuple(self._work_item(issue) for issue in issues[:limit])

    async def view_work_item(self, workspace_id: WorkspaceId, number: int) -> WorkItem:
        project = await self._project(workspace_id); issue = await self._api("GET", f"/projects/{project}/issues/{number}"); notes = await self._list(f"/projects/{project}/issues/{number}/notes")
        return self._work_item(issue, tuple(self._discussion(note) for note in notes))

    async def list_pipeline_status(self, workspace_id: WorkspaceId, *, ref: str | None = None, change_request_number: int | None = None) -> PipelineStatus:
        project = await self._project(workspace_id)
        if ref is None and change_request_number is not None:
            mr = await self._api("GET", f"/projects/{project}/merge_requests/{change_request_number}"); ref = self._str(mr,"sha")
        if not ref: raise ValueError("provide ref or change_request_number")
        pipelines = await self._list(f"/projects/{project}/pipelines", {"sha": ref})
        values: list[Pipeline] = []
        for pipeline in pipelines:
            pipeline_id = pipeline.get("id")
            if not isinstance(pipeline_id, int) or isinstance(pipeline_id, bool) or pipeline_id < 1:
                raise GitLabSourceControlError("GitLab returned an invalid pipeline id")
            values.append(Pipeline(pipeline_id, self._str(pipeline,"name") or "pipeline", self._str(pipeline,"status"), None, self._str(pipeline,"web_url")))
        return PipelineStatus(ref=ref, checks=(), pipelines=tuple(values))

    async def get_job_logs(self, workspace_id: WorkspaceId, pipeline_id: int, job_id: int | None = None) -> JobLogs:
        project = await self._project(workspace_id)
        if job_id is None:
            jobs = await self._list(f"/projects/{project}/pipelines/{pipeline_id}/jobs")
            candidates = [job for job in jobs if self._str(job, "status") == "failed"]
            candidates = candidates or [job for job in jobs if self._str(job, "status") in {"success", "failed", "canceled", "skipped"}]
            if not candidates or not isinstance(candidates[0].get("id"), int):
                raise GitLabSourceControlError("GitLab returned no completed jobs for pipeline")
            job_id = candidates[0]["id"]
        data = await self._transport.download(f"/projects/{project}/jobs/{job_id}/trace"); text = data.decode(errors="replace"); truncated = len(text)>_MAX_LOG_CHARACTERS
        return JobLogs(pipeline_id, job_id, text[-_MAX_LOG_CHARACTERS:] if truncated else text, truncated)

    async def retry_pipeline(self, workspace_id: WorkspaceId, pipeline_id: int, job_id: int | None = None) -> PipelineRetry:
        project = await self._project(workspace_id)
        if job_id is None: await self._api("POST", f"/projects/{project}/pipelines/{pipeline_id}/retry")
        else: await self._api("POST", f"/projects/{project}/jobs/{job_id}/retry")
        return PipelineRetry(pipeline_id, job_id)

    async def _root(self, workspace_id: WorkspaceId) -> str:
        if self._workspace_provider is None: raise GitLabSourceControlError("source control was composed without a workspace provider")
        return await self._workspace_provider.root_path(workspace_id)
    async def _project(self, workspace_id: WorkspaceId) -> str:
        remote = await self._checked(workspace_id,("remote","get-url","origin")); value=remote.removesuffix(".git")
        path = value.split(":",1)[1] if value.startswith("git@") else urlparse(value).path.lstrip("/")
        return quote(path, safe="")
    async def _git(self, root: str, args: Sequence[str]) -> GitResult:
        process=await asyncio.create_subprocess_exec("git","-C",root,*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,env={k:v for k,v in os.environ.items() if k not in {"GITLAB_TOKEN","GH_TOKEN","GITHUB_TOKEN"}}); out,err=await process.communicate(); return GitResult(process.returncode or 0,out.decode(errors="replace").strip(),err.decode(errors="replace").strip())
    async def _checked(self, workspace_id: WorkspaceId,args: Sequence[str]) -> str:
        result=await self._git(await self._root(workspace_id),args)
        if not result.ok: raise GitLabSourceControlError(result.stderr or result.stdout)
        return result.stdout
    async def _api(self,*args: object,**kwargs: object) -> dict:
        try:
            result=await self._transport.request(*args,**kwargs)
        except GitLabTransportError as error: raise GitLabSourceControlError(str(error)) from error
        return result if isinstance(result,dict) else {}
    async def _list(self,path: str,params: dict | None=None) -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            result=await self._transport.request("GET",path,params={**(params or {}), "per_page": 100, "page": page})
            current = [item for item in result if isinstance(item,dict)] if isinstance(result,list) else []
            items.extend(current)
            if len(current) < 100:
                return items
            page += 1
    @staticmethod
    def _str(value: dict,key: str) -> str: return value.get(key,"") if isinstance(value.get(key,""),str) else ""
    @classmethod
    def _nested(cls,value: dict,outer: str,key: str) -> str: return cls._str(value.get(outer,{}),key) if isinstance(value.get(outer),dict) else ""
    @classmethod
    def _discussion(cls,value: dict) -> Discussion: return Discussion(cls._nested(value,"author","username"),cls._str(value,"body"),cls._str(value,"web_url"))
    @classmethod
    def _work_item(cls,value: dict, comments: tuple[Discussion,...]=()) -> WorkItem: return WorkItem(int(value.get("iid",0)),cls._str(value,"title"),cls._str(value,"state"),cls._str(value,"description"),cls._nested(value,"author","username"),cls._str(value,"web_url"),tuple(str(x) for x in value.get("labels",[]) if isinstance(x,str)),comments)
    @staticmethod
    def _public(branch: str) -> None:
        if not branch.strip() or branch.startswith("engine/"): raise ValueError("branch must be a non-internal branch")
    @staticmethod
    def _mr_url(url: str) -> tuple[str,int]:
        parsed=urlparse(url); marker="/-/merge_requests/"; path=parsed.path
        if not parsed.hostname or marker not in path: raise ValueError("not a GitLab merge-request URL")
        project, tail = path.lstrip("/").split(marker,1)
        iid, separator, remainder = tail.partition("/")
        if not project or not iid.isdigit() or separator or remainder:
            raise ValueError("not a GitLab merge-request URL")
        return quote(project,safe=""),int(iid)


__all__ = ["GitLabSourceControl", "GitLabSourceControlError"]
