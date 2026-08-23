"""Planning hierarchy that groups workflow runs into product delivery work."""

from dataclasses import dataclass

from engine.domain.ids import MilestoneId, ProjectId, WorkstreamId


@dataclass(frozen=True, slots=True)
class Project:
    """An end-to-end product being delivered."""

    project_id: ProjectId
    name: str


@dataclass(frozen=True, slots=True)
class Milestone:
    """A delivery goal belonging to one project."""

    milestone_id: MilestoneId
    project_id: ProjectId
    name: str


@dataclass(frozen=True, slots=True)
class Workstream:
    """A body of workflow-run work belonging to one milestone."""

    workstream_id: WorkstreamId
    milestone_id: MilestoneId
    name: str


__all__ = ["Milestone", "Project", "Workstream"]
