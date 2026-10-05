"""Host-owned issue references shared by workspace and publishing paths."""

import re
from collections.abc import Mapping


def issue_reference(issue: Mapping[str, object], project: str) -> str:
    repository, number = issue.get("repository"), issue.get("number")
    if not isinstance(repository, str) or not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
        raise ValueError("issue.repository must be owner/repo")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise ValueError("issue.number must be a positive integer")
    return f"{'' if repository.lower() == project.lower() else repository}#{number}"


def issue_body(
    body: str, reference: str, resolution: str | None, *, qualified_reference: str = ""
) -> str:
    """Replace closing/reference keywords for this issue with one explicit line."""
    if not isinstance(resolution, str) or resolution not in {"resolves", "refs"}:
        raise ValueError("issue_resolution is required for issue work: choose resolves or refs")
    references = "|".join(re.escape(value) for value in {reference, qualified_reference} if value)
    pattern = rf"(?i)\b(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?|refs)\s+:?\s*(?:{references})(?![\w/])"
    cleaned = re.sub(pattern, "", body).strip()
    return f"{cleaned}\n\n{resolution.capitalize()} {reference}".lstrip()
