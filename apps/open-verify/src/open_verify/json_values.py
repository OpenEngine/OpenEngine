"""Type-sensitive JSON comparison shared by live and exported browser checks."""

from pydantic import JsonValue


def json_equal(actual: JsonValue, expected: JsonValue) -> bool:
    """Compare every JSON value's type, including booleans nested inside containers."""
    if type(actual) is not type(expected):
        return False
    if isinstance(actual, dict):
        return actual.keys() == expected.keys() and all(
            json_equal(value, expected[key]) for key, value in actual.items()
        )
    if isinstance(actual, list):
        return len(actual) == len(expected) and all(
            json_equal(left, right) for left, right in zip(actual, expected, strict=True)
        )
    return actual == expected
