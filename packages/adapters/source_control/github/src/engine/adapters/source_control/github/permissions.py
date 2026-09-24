"""Shared repository access policy for browser login and PR interactions."""

from collections.abc import Awaitable, Callable
from urllib.parse import quote


async def can_write_repository(
    request: Callable[[str, str], Awaitable[object]],
    repository: str,
    login: str,
    *,
    user_id: int | None = None,
) -> bool:
    """Check effective access, optionally binding it to a verified user ID.

    The caller supplies its privileged API request function. Lookup errors
    propagate so callers can distinguish failures from cacheable denials.
    """
    response = await request(
        "GET", f"/repos/{repository}/collaborators/{quote(login, safe='')}/permission"
    )
    # GitHub normally maps maintain to write; accept either representation.
    # Unknown/missing permissions never grant access.
    if not isinstance(response, dict) or response.get("permission") not in ("write", "maintain", "admin"):
        return False
    if user_id is None:
        return True
    user = response.get("user")
    return (
        type(user_id) is int and user_id > 0
        and isinstance(user, dict)
        and type(user.get("id")) is int and user["id"] == user_id
        and isinstance(user.get("login"), str)
        and user["login"].lower() == login.lower()
    )
