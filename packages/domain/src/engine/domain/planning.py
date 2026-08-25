"""Planning hierarchy that groups workflow runs into product delivery work."""

from dataclasses import dataclass, field

from engine.domain.ids import AgentInstanceId, MilestoneId, ProjectId, WorkstreamId


@dataclass(frozen=True, slots=True)
class Project:
    """An end-to-end product being delivered."""

    project_id: ProjectId
    name: str
    archived: bool = False
    """Put away rather than deleted: still listed, under its own heading, and
    restored by the same click that put it there."""


_INSTANCE_PROJECT_PREFIX = "project-"


def project_id_for_instance(instance_id: AgentInstanceId) -> ProjectId:
    """Return the durable project owned by a New Project conversation."""
    return ProjectId(f"{_INSTANCE_PROJECT_PREFIX}{instance_id}")


def instance_id_for_project(project_id: ProjectId) -> AgentInstanceId | None:
    """Return the conversation `project_id` was named after, if it was.

    The inverse of `project_id_for_instance`, and a guess rather than a lookup:
    a project recorded some other way can share the shape without owning a
    conversation, so callers must confirm the instance exists before trusting
    it.
    """
    if not project_id.startswith(_INSTANCE_PROJECT_PREFIX):
        return None
    instance_id = project_id[len(_INSTANCE_PROJECT_PREFIX) :]
    return AgentInstanceId(instance_id) if instance_id else None


@dataclass(frozen=True, slots=True)
class Milestone:
    """A delivery goal belonging to one project."""

    milestone_id: MilestoneId
    project_id: ProjectId
    name: str
    description: str = ""
    dependencies: tuple[MilestoneId, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class Workstream:
    """A body of workflow-run work belonging to one milestone."""

    workstream_id: WorkstreamId
    milestone_id: MilestoneId
    name: str


__all__ = [
    "Milestone",
    "Project",
    "Workstream",
    "instance_id_for_project",
    "project_id_for_instance",
]
