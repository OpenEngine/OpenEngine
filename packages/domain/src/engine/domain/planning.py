"""Planning hierarchy that groups workflow runs into product delivery work."""

from dataclasses import dataclass, field

from engine.domain.ids import AgentInstanceId, MilestoneId, ProjectId, WorkstreamId


@dataclass(frozen=True, slots=True)
class Project:
    """An end-to-end product being delivered."""

    project_id: ProjectId
    name: str


def project_id_for_instance(instance_id: AgentInstanceId) -> ProjectId:
    """Return the durable project owned by a New Project conversation."""
    return ProjectId(f"project-{instance_id}")


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


__all__ = ["Milestone", "Project", "Workstream", "project_id_for_instance"]
