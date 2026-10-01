"""What a review of a change and its findings are called on both sides.

A workflow started in review and `engine review`, which starts and answers it,
agree on these names; a rename on one side alone would leave the other
dropping inputs or waiting on a question that is never asked.

A finding reads the same whether the reranker posts it or a person does from
`engine review`, so a comment says the same thing whoever sent it.
"""

from __future__ import annotations

from collections.abc import Container, Mapping
from enum import StrEnum

from engine.domain.forge import MODE_INPUT, ForgeMode
from engine.domain.states import STATE_INPUT, WorkState

#: The tool name a triage question is raised under, so a client can tell the
#: choice of findings to fix from a verdict or an agent's permission request.
TRIAGE_TOOL = "findings_triage"

#: The creation inputs naming the change a run started in review looks at: the
#: ref checked out, its pull request, and that pull request's branch a fix is
#: pushed to (the workspace's own branch is not it).
REVIEW_REF_INPUT = "ref"
REVIEW_PR_INPUT = "pr_url"
REVIEW_BRANCH_INPUT = "branch"
#: The creation input saying what a run started in review leaves on its pull request.
REVIEW_PUBLISH_INPUT = "publish_review"


class ReviewPublishing(StrEnum):
    """What a run started in review leaves on the pull request it reviews.

    `engine review` keeps the findings for the person who asked; a review
    requested on the pull request itself is answered there, with comments and,
    where the deployment allows it, an approval when nothing survives.
    """

    KEEP = "keep"
    COMMENT = "comment"
    APPROVE = "approve"


def review_publishing(inputs: object) -> ReviewPublishing:
    """What a run with these creation inputs publishes; keeping when unsaid or unknown."""
    value = inputs.get(REVIEW_PUBLISH_INPUT) if isinstance(inputs, Mapping) else None
    try:
        return ReviewPublishing(value)
    except ValueError:
        return ReviewPublishing.KEEP


def review_inputs(
    declared: Container[str], *, ref: str, pr_url: str, branch: str,
    publishing: ReviewPublishing = ReviewPublishing.KEEP,
) -> dict[str, str]:
    """The creation inputs of a run started in review, limited to those `declared`.

    Connected only with a pull request branch: without one there is nothing
    to push a fix to or wait on CI for, nor anywhere to publish to.
    """
    connected = bool(pr_url and branch)
    inputs = {
        STATE_INPUT: str(WorkState.REVIEW),
        REVIEW_REF_INPUT: ref,
        REVIEW_PR_INPUT: pr_url,
        REVIEW_BRANCH_INPUT: branch,
        MODE_INPUT: str(ForgeMode.CONNECTED if connected else ForgeMode.DISCONNECTED),
        REVIEW_PUBLISH_INPUT: str(publishing if connected else ReviewPublishing.KEEP),
    }
    return {name: value for name, value in inputs.items() if name in declared}


def finding_comment(tagline: str, description: str, *, agent: str = "", facet: str = "") -> str:
    """A finding as a PR comment, with the lineage it was produced by."""
    parts = [f"**{tagline}**", "", description]
    lineage = [f"{name}: {value}" for name, value in (("agent", agent), ("facet", facet)) if value]
    if lineage:
        parts.extend(["", f"_Produced by {', '.join(lineage)}_"])
    return "\n".join(parts)


__all__ = [
    "REVIEW_BRANCH_INPUT",
    "REVIEW_PR_INPUT",
    "REVIEW_PUBLISH_INPUT",
    "REVIEW_REF_INPUT",
    "TRIAGE_TOOL",
    "ReviewPublishing",
    "finding_comment",
    "review_inputs",
    "review_publishing",
]
