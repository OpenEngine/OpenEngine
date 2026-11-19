"""The request helpers every Starlette app reads its JSON bodies with."""

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from engine.graph_runtime.http import (
    InvalidFieldError,
    error,
    json_body,
    optional_string,
    required_string,
)


async def _echo(request: Request) -> JSONResponse:
    return JSONResponse(await json_body(request))


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (b'{"a": 1}', {"a": 1}),
        (b"[1, 2]", {}),
        (b"not json", {}),
        (b"\xff\xfe\xfa", {}),
    ],
)
def test_a_body_that_is_not_a_json_object_reads_as_empty(content: bytes, expected: dict) -> None:
    client = TestClient(Starlette(routes=[Route("/", _echo, methods=["POST"])]))
    response = client.post("/", content=content)
    assert response.status_code == 200 and response.json() == expected


def test_a_required_string_is_stripped_and_a_blank_one_refused() -> None:
    assert required_string({"name": "  x "}, "name") == "x"
    for body in ({}, {"name": " "}, {"name": 3}):
        with pytest.raises(ValueError, match="^name must be a non-empty string$"):
            required_string(body, "name")


def test_an_absent_optional_string_is_the_default_and_a_blank_one_refused() -> None:
    assert optional_string({}, "name") is None
    assert optional_string({"name": None}, "name", default="") == ""
    assert optional_string({"name": " x"}, "name", default="") == "x"
    with pytest.raises(InvalidFieldError, match="^name must be a non-empty string$"):
        optional_string({"name": ""}, "name")


def test_an_error_is_its_message_under_error() -> None:
    response = error("no such run", 404)
    assert response.status_code == 404 and response.body == b'{"error":"no such run"}'
