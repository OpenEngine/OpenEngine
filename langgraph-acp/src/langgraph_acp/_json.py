"""JSON-shaped values: the copies that isolate them, and the reads that check them.

ACP speaks JSON-RPC, so a few fields here carry whatever the agent sent: event
payloads, result content, config values. Typing those as `JSONValue` states that
honestly rather than inventing a richer type before the tickets that normalize
them exist.

Mappings and sequences are copied on the way in and on the way out. These
objects end up in LangGraph state and in streamed events, where a container
still shared with the caller is a mutation arriving from somewhere else
entirely. The copy is *deep*, because the shape this data actually has is
nested -- an ACP `session/update` is `{"update": {...}}`, and a shallow copy
would leave the interesting part shared while the docstring claimed otherwise.
The cost is proportional to the number of containers, not to the text inside
them: strings are immutable and are shared rather than duplicated.

The copies produce plain `dict` and `tuple` rather than read-only views because
LangGraph checkpointing pickles what it stores, and `MappingProxyType` cannot be
pickled.

A lone `str` is refused wherever a sequence is expected. It is iterable, so
without the guard `additional_directories="/repos/docs"` becomes eleven
single-character roots and the mistake surfaces as a bad ACP request much later.

The `as_*` readers exist for `from_dict`, where the input is whatever a store or
a checkpoint handed back. They check rather than coerce: a token count that
returns as `"1200"` should fail at the boundary it crossed, not as an arithmetic
error in whatever later sums it.
"""

from collections.abc import Iterable, Mapping
from copy import deepcopy

type JSONValue = str | int | float | bool | None | Iterable[JSONValue] | Mapping[str, JSONValue]

#: What a `to_dict` returns: a mapping the caller may keep and mutate freely.
type JSONObject = dict[str, JSONValue]


def copied_mapping(values: Mapping[str, JSONValue] | None) -> JSONObject:
    """A private `dict`, nested containers included, holding what `values` held."""
    return deepcopy(dict(values or {}))


def checked_sequence[T](values: Iterable[T] | None, *, field: str) -> tuple[T, ...]:
    """The values as a tuple, refusing the string that is quietly a sequence."""
    if isinstance(values, (str, bytes)):
        raise TypeError(
            f"{field} takes a sequence of values, not the single "
            f"{type(values).__name__} {values!r}: iterating one yields a "
            "separate entry per character, which is never what was meant"
        )
    return tuple(values or ())


def copied_sequence(
    values: Iterable[JSONValue] | None, *, field: str
) -> tuple[JSONValue, ...]:
    """A private tuple, nested containers included, holding what `values` held."""
    return tuple(deepcopy(value) for value in checked_sequence(values, field=field))


def _wrong(field: str, expected: str, value: object) -> TypeError:
    return TypeError(
        f"{field} must be {expected}, not the {type(value).__name__} {value!r}"
    )


def as_str(value: object, *, field: str) -> str:
    """`value` as the string it should already be."""
    if isinstance(value, str):
        return value
    raise _wrong(field, "a string", value)


def as_optional_str(value: object, *, field: str) -> str | None:
    return None if value is None else as_str(value, field=field)


def as_optional_int(value: object, *, field: str) -> int | None:
    # `bool` is an `int` in Python, and is never a token count.
    if value is None or (isinstance(value, int) and not isinstance(value, bool)):
        return value
    raise _wrong(field, "a whole number", value)


def as_optional_float(value: object, *, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    raise _wrong(field, "a number", value)


def as_mapping(value: object, *, field: str) -> JSONObject:
    """`value` as a private mapping. A missing key reads as an empty one."""
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return copied_mapping(value)
    raise _wrong(field, "a mapping", value)


def as_sequence(value: object, *, field: str) -> tuple[JSONValue, ...]:
    """`value` as a private tuple. A missing key reads as an empty one."""
    if value is None:
        return ()
    if not isinstance(value, Iterable):
        raise _wrong(field, "a sequence of values", value)
    return copied_sequence(value, field=field)


__all__ = [
    "JSONObject",
    "JSONValue",
    "as_mapping",
    "as_optional_float",
    "as_optional_int",
    "as_optional_str",
    "as_sequence",
    "as_str",
    "checked_sequence",
    "copied_mapping",
    "copied_sequence",
]
