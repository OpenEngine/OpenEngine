"""What an `ACPSessionStore` has to be, in a form every implementation runs.

`isinstance(store, ACPSessionStore)` is not that check and cannot become one: a
`runtime_checkable` protocol compares member *names* and nothing else. An object
whose three methods are synchronous is an instance of it. So is one keyed by the
`(namespace, key)` pair LangGraph's own `BaseStore` uses. So is this package's
own store with `thread_id` renamed to `thread` -- which passes an isinstance
test, passes mypy strict as well (a protocol's parameters are matched
positionally), and raises `TypeError` in the first node that calls it, because
every caller passes these arguments by keyword.

What a caller actually depends on is the signature it writes and the lifecycle
it gets back, so those are the two checks here. Ticket 17's durable stores --
LangGraph-backed, SQLite, Postgres -- run the same two functions against their
own instances; that is why this is an importable module rather than a block of
assertions inside `test_store.py`.
"""

import inspect
from collections.abc import Callable
from typing import Any

from langgraph_acp import ACPSessionStore

#: The interface, which `test_store.py` separately pins at three methods.
METHODS = ("get", "put", "delete")


def _parameters(method: Callable[..., Any]) -> list[tuple[str, str]]:
    """Names and kinds, in order. Kinds matter as much as names: a
    positional-only parameter refuses the keyword call every caller writes."""
    return [
        (parameter.name, parameter.kind.name)
        for parameter in inspect.signature(method).parameters.values()
    ]


def assert_conforms(store: object) -> None:
    """Every method the protocol declares, awaitable, with its parameters."""
    implementation = type(store).__name__
    for name in METHODS:
        declared = getattr(ACPSessionStore, name)
        implemented = getattr(type(store), name, None)
        assert implemented is not None, f"{implementation} has no {name}()"
        assert inspect.iscoroutinefunction(implemented), (
            f"{implementation}.{name}() is not awaitable"
        )
        assert _parameters(implemented) == _parameters(declared), (
            f"{implementation}.{name}{inspect.signature(implemented)} "
            f"is not {name}{inspect.signature(declared)}"
        )


async def assert_lifecycle(store: ACPSessionStore) -> None:
    """Bind, read, rebind, forget -- every call written the way a node writes it.

    By keyword deliberately, and this is the only check that catches a renamed
    parameter: mypy matches a protocol's parameters positionally, so passing a
    store whose `thread_id` is spelled `thread` to this very function type checks
    clean. What such a store cannot survive is being called, so it is called.

    The store must not already hold the identity this uses.
    """
    thread_id, session_key = "conformance-thread", "conformance-key"

    assert await store.get(thread_id=thread_id, session_key=session_key) is None

    await store.put(
        thread_id=thread_id, session_key=session_key, acp_session_id="sess_first"
    )
    assert await store.get(thread_id=thread_id, session_key=session_key) == "sess_first"

    await store.put(
        thread_id=thread_id, session_key=session_key, acp_session_id="sess_second"
    )
    assert await store.get(thread_id=thread_id, session_key=session_key) == "sess_second"

    await store.delete(thread_id=thread_id, session_key=session_key)
    assert await store.get(thread_id=thread_id, session_key=session_key) is None

    # Deleting a binding that is not there is not an error.
    await store.delete(thread_id=thread_id, session_key=session_key)
