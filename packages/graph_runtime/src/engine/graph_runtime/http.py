"""Request parsing and error replies shared by the Starlette apps.

`engine.graph_runtime.api` and `engine.apps.web.api` read JSON bodies and
refuse bad fields the same way, so they read them with the same helpers. They
live here rather than in `engine.runtime` because this package already owns an
HTTP surface and depends on Starlette, and `engine.runtime` depends on it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import overload

from starlette.requests import Request
from starlette.responses import JSONResponse


class InvalidFieldError(ValueError):
    """A field a request body needs is absent, blank or not text."""

    def __init__(self, name: str) -> None:
        super().__init__(f"{name} must be a non-empty string")


async def json_body(request: Request) -> dict[str, object]:
    """The body as a JSON object, or `{}` when it is not one."""
    try:
        body = await request.json()
    except ValueError:
        # `ValueError` rather than `JSONDecodeError`: Starlette hands raw bytes
        # to `json.loads`, which decodes them itself, so a body that is not
        # UTF-8 raises `UnicodeDecodeError`. Both are `ValueError`, and letting
        # one of them past here is the difference between the 400 every other
        # unreadable body gets and a 500.
        return {}
    return body if isinstance(body, dict) else {}


def required_string(body: Mapping[str, object], name: str) -> str:
    """The field, stripped. Absent, blank or not text is a client's mistake."""
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise InvalidFieldError(name)
    return value.strip()


@overload
def optional_string(body: Mapping[str, object], name: str) -> str | None: ...


@overload
def optional_string(body: Mapping[str, object], name: str, default: str) -> str: ...


def optional_string(
    body: Mapping[str, object], name: str, default: str | None = None
) -> str | None:
    """The field, or `default` when it is absent. A blank one is a client's mistake."""
    if body.get(name) is None:
        return default
    return required_string(body, name)


def error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


__all__ = [
    "InvalidFieldError",
    "error",
    "json_body",
    "optional_string",
    "required_string",
]
