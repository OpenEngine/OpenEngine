"""The session binding: its lifecycle, and the isolation it has to guarantee.

Three things are worth pinning down here. The lifecycle -- put, get, delete --
is the acceptance criterion. The isolation is the reason the store is keyed by a
pair: one LangGraph thread runs an implementer and three reviewers, and a node
that resumed the wrong conversation would be a plausible-looking agent replying
to somebody else's history. And conformance is checked against the interface
rather than against this one class, because swapping in a durable store is meant
to be a constructor argument, and the durable stores are a later ticket that
will find these checks already written.

What is deliberately not tested is durability, because this implementation has
none. It is the store that lives as long as its process, and the ones that
outlive a restart arrive with that later ticket.
"""

import asyncio
import inspect
from collections.abc import Callable, Coroutine
from functools import wraps
from typing import Any

import pytest

from langgraph_acp import ACPSessionStore, InMemoryACPSessionStore


def asyncio_test(
    test: Callable[..., Coroutine[Any, Any, None]],
) -> Callable[..., None]:
    """Run an async test. A decorator rather than a plugin, so that the package
    keeps the empty dependency list its first ticket established."""

    @wraps(test)
    def synchronously(*args: Any, **kwargs: Any) -> None:
        asyncio.run(test(*args, **kwargs))

    return synchronously


@asyncio_test
async def test_a_binding_round_trips() -> None:
    store = InMemoryACPSessionStore()

    await store.put("pr-918", "reviewer", "sess_abc123")

    assert await store.get("pr-918", "reviewer") == "sess_abc123"


@asyncio_test
async def test_an_unbound_identity_reads_as_none() -> None:
    """The first-invocation answer: create a session, then bind what comes back."""
    store = InMemoryACPSessionStore()

    assert await store.get("pr-918", "reviewer") is None


@asyncio_test
async def test_deleting_forgets_the_binding() -> None:
    store = InMemoryACPSessionStore()
    await store.put("pr-918", "reviewer", "sess_abc123")

    await store.delete("pr-918", "reviewer")

    assert await store.get("pr-918", "reviewer") is None


@asyncio_test
async def test_deleting_a_binding_that_is_not_there_is_not_an_error() -> None:
    """Cleanup runs on paths that do not know whether a session was ever bound."""
    store = InMemoryACPSessionStore()

    await store.delete("pr-918", "reviewer")


@asyncio_test
async def test_session_keys_coexist_under_one_thread() -> None:
    """One pull request, four agents, four conversations that never cross."""
    store = InMemoryACPSessionStore()

    await store.put("pr-918", "implementer", "sess_a")
    await store.put("pr-918", "reviewer-1", "sess_b")
    await store.put("pr-918", "reviewer-2", "sess_c")
    await store.put("pr-918", "security", "sess_d")

    assert await store.get("pr-918", "implementer") == "sess_a"
    assert await store.get("pr-918", "reviewer-1") == "sess_b"
    assert await store.get("pr-918", "reviewer-2") == "sess_c"
    assert await store.get("pr-918", "security") == "sess_d"


@asyncio_test
async def test_deleting_one_binding_leaves_its_siblings() -> None:
    store = InMemoryACPSessionStore()
    await store.put("pr-918", "implementer", "sess_a")
    await store.put("pr-918", "reviewer", "sess_b")

    await store.delete("pr-918", "reviewer")

    assert await store.get("pr-918", "implementer") == "sess_a"


@asyncio_test
async def test_one_key_in_two_threads_is_two_bindings() -> None:
    """The reviewer on one pull request is not the reviewer on another."""
    store = InMemoryACPSessionStore()

    await store.put("pr-918", "reviewer", "sess_918")
    await store.put("pr-919", "reviewer", "sess_919")

    assert await store.get("pr-918", "reviewer") == "sess_918"
    assert await store.get("pr-919", "reviewer") == "sess_919"


@asyncio_test
async def test_an_identity_is_the_pair_and_not_the_two_strings_joined() -> None:
    """A key built by concatenation would collide across the separator."""
    store = InMemoryACPSessionStore()

    await store.put("pr-918:security", "reviewer", "sess_one")
    await store.put("pr-918", "security:reviewer", "sess_two")

    assert await store.get("pr-918:security", "reviewer") == "sess_one"
    assert await store.get("pr-918", "security:reviewer") == "sess_two"


@asyncio_test
async def test_rebinding_replaces_the_earlier_session() -> None:
    """What a node writes down after `strategy="new"` starts a fresh conversation."""
    store = InMemoryACPSessionStore()
    await store.put("pr-918", "reviewer", "sess_old")

    await store.put("pr-918", "reviewer", "sess_new")

    assert await store.get("pr-918", "reviewer") == "sess_new"


@asyncio_test
async def test_a_store_can_be_seeded_with_bindings() -> None:
    store = InMemoryACPSessionStore({("pr-918", "reviewer"): "sess_abc123"})

    assert await store.get("pr-918", "reviewer") == "sess_abc123"


@asyncio_test
async def test_seeded_bindings_are_copied_rather_than_shared() -> None:
    seed = {("pr-918", "reviewer"): "sess_abc123"}
    store = InMemoryACPSessionStore(seed)

    seed[("pr-918", "reviewer")] = "MUTATED"

    assert await store.get("pr-918", "reviewer") == "sess_abc123"


#: Every implementation checked against the interface. Ticket 17's durable
#: stores add themselves here, and inherit the conformance checks below.
IMPLEMENTATIONS: tuple[type[Any], ...] = (InMemoryACPSessionStore,)


def declares_static_conformance(store: InMemoryACPSessionStore) -> ACPSessionStore:
    """The half of conformance `isinstance` cannot see.

    `runtime_checkable` checks that three names exist and stops there, so an
    object whose methods are synchronous, or which takes `(namespace, key)` the
    way LangGraph's `BaseStore` does, satisfies `isinstance` while breaking
    every caller. This function is never called: mypy checks the return, and
    dropping an `async`, changing a type, or taking a fourth argument fails the
    type check instead of production.

    Parameter *names* it will not catch -- mypy ignores them when matching a
    protocol -- which is what the signature check below is for.
    """
    return store


@pytest.mark.parametrize(
    "implementation", IMPLEMENTATIONS, ids=lambda cls: str(cls.__name__)
)
def test_an_implementation_is_an_acp_session_store(implementation: type[Any]) -> None:
    assert isinstance(implementation(), ACPSessionStore)


@pytest.mark.parametrize(
    "implementation", IMPLEMENTATIONS, ids=lambda cls: str(cls.__name__)
)
def test_an_implementation_is_callable_the_way_the_protocol_says(
    implementation: type[Any],
) -> None:
    """Signatures match exactly, parameter names included.

    Callers pass `thread_id=` and `session_key=` by keyword, and swapping one
    store for another is supposed to be a constructor argument. A renamed
    parameter keeps `isinstance` happy and every existing test green while
    raising `TypeError` in whichever deployment configured the other store.
    """
    for name in ("get", "put", "delete"):
        declared = getattr(ACPSessionStore, name)
        implemented = getattr(implementation, name)

        assert inspect.iscoroutinefunction(implemented), f"{name} is not awaitable"
        assert inspect.signature(implemented) == inspect.signature(declared), (
            f"{name} does not have the signature the protocol declares"
        )


def test_the_interface_is_three_methods_and_no_more() -> None:
    """The store holds session identifiers. History belongs to the agent, and an
    interface with somewhere to put a transcript is an invitation to keep one."""
    declared = {name for name in vars(ACPSessionStore) if not name.startswith("_")}

    assert declared == {"get", "put", "delete"}
