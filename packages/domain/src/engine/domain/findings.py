"""What a review of a change and its findings are called on both sides.

A workflow started in review and `engine review`, which starts and answers it,
agree on these names; a rename on one side alone would leave the other
dropping inputs or waiting on a question that is never asked.

A finding reads the same whether the reranker posts it or a person does from
`engine review`, so a comment says the same thing whoever sent it.
"""

from __future__ import annotations

#: The tool name a triage question is raised under, so a client can tell the
#: choice of findings to fix from a verdict or an agent's permission request.
TRIAGE_TOOL = "findings_triage"

#: The creation inputs naming the change a run started in review looks at: the
#: ref checked out, its pull request, and that pull request's branch a fix is
#: pushed to (the workspace's own branch is not it).
REVIEW_REF_INPUT = "ref"
REVIEW_PR_INPUT = "pr_url"
REVIEW_BRANCH_INPUT = "branch"


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
    "REVIEW_REF_INPUT",
    "TRIAGE_TOOL",
    "finding_comment",
]
