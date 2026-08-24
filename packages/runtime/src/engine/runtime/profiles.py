"""The agents this system ships with.

Profiles are configuration, so they are values, not classes -- adding an agent
is adding an entry here, and nothing about it is special-cased anywhere else.
This lives in the runtime rather than the domain because it is a *choice* about
which agents exist; `engine.domain.agents` owns what an agent **is**.

A profile deliberately does not name the runner that executes it. Which runner
that is comes from `Capabilities.agent_runner`, chosen by the composition root,
so the same profile is a Codex agent in one process and something else in
another without being edited.
"""

from collections.abc import Mapping
from dataclasses import replace

from engine.domain.agents import AgentProfile
from engine.domain.ids import AgentId

FOREMAN = AgentProfile(
    agent_id=AgentId("foreman"),
    instructions=(
        "You are the foreman of a team of coding agents. Coordinate implementation "
        "work, answer questions from coders, and escalate to a human when a "
        "decision is not yours to make.\n\n"
        "You cannot yet dispatch work or author workflows -- those tools are not "
        "built. Until they are, say plainly what you would do and who would do it, "
        "rather than implying anything has been started."
    ),
    # Empty on purpose. The grants this profile wants -- "dispatch",
    # "author_workflow" -- resolve to no tool today, and `AgentSession` refuses
    # to run a profile whose grants it cannot honour rather than quietly
    # dropping them. They go in when the tools do.
    capabilities=(),
    description="Coordinates implementation work and escalates decisions.",
)

CODER = AgentProfile(
    agent_id=AgentId("coder"),
    instructions=(
        "You are a coding agent working in a checkout of this repository. Read "
        "before you answer, prefer the smallest change that solves the problem, "
        "and say what you are uncertain about rather than guessing."
    ),
    capabilities=(),
    description="Reads and reasons about the code in the working tree.",
)

PLANNER = AgentProfile(
    agent_id=AgentId("planner"),
    instructions=(
        "You are a planning agent working in a checkout of this repository. Read "
        "whatever you need to understand the request, then write the plan a coder "
        "would work from: what to change, where, in what order, and what you are "
        "unsure of. The plan is the deliverable -- do not change the workspace, "
        "and say what you would do rather than implying anything has been done."
    ),
    capabilities=(),
    description="Reads the code and writes the plan for a change.",
    # What holds it to reading is the runner it gets and the approvals it is
    # refused, neither of which is the paragraph above. Stated here so both can
    # act on it.
    read_only=True,
)

#: Every profile the system knows, by id.
BUILT_IN: Mapping[AgentId, AgentProfile] = {
    profile.agent_id: profile for profile in (FOREMAN, CODER, PLANNER)
}

#: Introduces the granted tool names appended to a profile's instructions.
#: A grant is not an announcement: resolving one puts the tool in front of the
#: provider and says nothing to the model, so an agent reads its role
#: description as the whole story -- a planner told its plan is the deliverable
#: holds milestone tools and still answers with what it *would* record. The
#: system prompt is where that gap closes.
GRANTED_TOOLS_NOTE = (
    "These tools are granted to you for this conversation, on top of the ones "
    "your provider always offers. Use them when the work calls for them rather "
    "than describing what you would do with them. Your provider may expose "
    "them under a prefixed name."
)


def with_granted_tools(profile: AgentProfile) -> AgentProfile:
    """The same profile, with its instructions naming what it was granted.

    Names only, unprefixed: how a provider spells a tool is the adapter's
    business, and the runtime writing `mcp__planning__add_milestone` would be
    the runtime claiming to know which provider is answering.

    A profile granted nothing is returned untouched, so the note never appears
    announcing an empty set.
    """
    if not profile.capabilities:
        return profile
    granted = "\n".join(f"- {name}" for name in profile.capabilities)
    sections = (profile.instructions.strip(), f"{GRANTED_TOOLS_NOTE}\n\n{granted}")
    return replace(
        profile,
        instructions="\n\n".join(section for section in sections if section),
    )


class UnknownAgentError(KeyError):
    """No profile is registered under that id."""

    def __init__(self, agent_id: AgentId, known: Mapping[AgentId, AgentProfile]) -> None:
        super().__init__(f"no agent profile {agent_id!r}; known: {sorted(known)}")
        self.agent_id = agent_id


def profile_for(agent_id: AgentId, profiles: Mapping[AgentId, AgentProfile] = BUILT_IN) -> AgentProfile:
    """Look up one profile, or say which ones exist."""
    try:
        return profiles[agent_id]
    except KeyError:
        raise UnknownAgentError(agent_id, profiles) from None


__all__ = [
    "BUILT_IN",
    "CODER",
    "FOREMAN",
    "GRANTED_TOOLS_NOTE",
    "PLANNER",
    "UnknownAgentError",
    "profile_for",
    "with_granted_tools",
]
