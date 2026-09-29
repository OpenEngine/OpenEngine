"""How a review finding reads when it is posted on a change request.

One wording, whether the reranker posts a finding or a person does from
`engine review`, so a comment says the same thing whoever sent it.
"""

from __future__ import annotations


def finding_comment(tagline: str, description: str, *, agent: str = "", facet: str = "") -> str:
    """A finding as a PR comment, with the lineage it was produced by."""
    parts = [f"**{tagline}**", "", description]
    lineage = [f"{name}: {value}" for name, value in (("agent", agent), ("facet", facet)) if value]
    if lineage:
        parts.extend(["", f"_Produced by {', '.join(lineage)}_"])
    return "\n".join(parts)


__all__ = ["finding_comment"]
