"""JSON-shaped values, and the copy that keeps them from being shared.

ACP speaks JSON-RPC, so a few fields here carry whatever the agent sent: event
payloads, result content, config values. Typing those as `JSONValue` states that
honestly rather than inventing a richer type before the tickets that normalize
them exist.

Mappings and sequences are copied on construction. These objects end up in
LangGraph state and in streamed events, where a container still shared with the
caller is a mutation arriving from somewhere else entirely. The copy is a plain
`dict` rather than a read-only view because LangGraph checkpointing pickles what
it stores, and `MappingProxyType` cannot be pickled.
"""

from collections.abc import Iterable, Mapping

type JSONValue = str | int | float | bool | None | Iterable[JSONValue] | Mapping[str, JSONValue]

#: What a `to_dict` returns: a mapping the caller may keep and mutate freely.
type JSONObject = dict[str, JSONValue]


def copied_mapping(values: Mapping[str, JSONValue] | None) -> JSONObject:
    """A private `dict` holding what `values` held."""
    return dict(values or {})


def copied_sequence(values: Iterable[JSONValue] | None) -> tuple[JSONValue, ...]:
    """A private tuple holding what `values` held."""
    return tuple(values or ())


__all__ = ["JSONObject", "JSONValue", "copied_mapping", "copied_sequence"]
