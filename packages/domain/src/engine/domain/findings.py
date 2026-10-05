"""What a review of a change and its findings are called on both sides.

A workflow started in review and `engine review`, which starts and answers it,
agree on these names; a rename on one side alone would leave the other
dropping inputs or waiting on a question that is never asked.

A finding reads the same whether the reranker posts it or a person does from
`engine review`, so a comment says the same thing whoever sent it.
"""

from __future__ import annotations

from collections.abc import Container

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
#: Set when whoever asked for the review reads it on the pull request rather
#: than at triage: Engine requested as a reviewer on GitHub.
REVIEW_PUBLISH_INPUT = "publish_review"


def review_inputs(
    declared: Container[str], *, ref: str, pr_url: str, branch: str, publish: bool = False,
) -> dict[str, str]:
    """The creation inputs of a run started in review, limited to those `declared`.

    Connected only with a pull request branch: without one there is nothing
    to push a fix to or wait on CI for. `publish` posts the review to that pull
    request instead of stopping at triage.
    """
    inputs = {
        STATE_INPUT: str(WorkState.REVIEW),
        REVIEW_REF_INPUT: ref,
        REVIEW_PR_INPUT: pr_url,
        REVIEW_BRANCH_INPUT: branch,
        MODE_INPUT: str(ForgeMode.CONNECTED if pr_url and branch else ForgeMode.DISCONNECTED),
    }
    if publish:
        inputs[REVIEW_PUBLISH_INPUT] = "true"
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
    "finding_comment",
    "review_inputs",
]
