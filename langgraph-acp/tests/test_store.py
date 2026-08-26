"""The session binding: its lifecycle, and the isolation it has to guarantee.

Two things are worth pinning down here. The lifecycle -- put, get, delete -- is
the acceptance criterion, and the isolation is the reason the store is keyed by
a pair: one LangGraph thread runs an implementer and three reviewers, and a node
that resumed the wrong conversation would be a plausible-looking agent replying
to somebody else's history.

What is deliberately not tested is durability, because this implementation has
none. It is the store that lives as long as its process, and the ones that
outlive a restart arrive with a later ticket.

Whether this store *is* an `ACPSessionStore` is asked of `store_conformance.py`,
which every implementation runs; the stores at the foot of this file are what
proves that check has teeth, and that an isinstance test does not.
"""

import pytest
from conftest import asyncio_test
from store_conformance import assert_conforms, assert_lifecycle

from langgraph_acp import ACPSessionStore, InMemoryACPSessionStore


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


def test_the_in_memory_store_conforms_to_the_interface() -> None:
    assert_conforms(InMemoryACPSessionStore())


@asyncio_test
async def test_the_in_memory_store_serves_the_whole_lifecycle() -> None:
    """The conformance suite's own run of it, called entirely by keyword."""
    await assert_lifecycle(InMemoryACPSessionStore())


class SynchronousStore:
    """Three methods with the right names and no `await` in sight."""

    def get(self, thread_id: str, session_key: str) -> str | None:
        return None

    def put(self, thread_id: str, session_key: str, acp_session_id: str) -> None:
        return None

    def delete(self, thread_id: str, session_key: str) -> None:
        return None


class NamespacedStore:
    """Keyed the way LangGraph's own `BaseStore` is, which is not this pair."""

    async def get(self, namespace: str, key: str) -> str | None:
        return None

    async def put(self, namespace: str, key: str, value: str) -> None:
        return None

    async def delete(self, namespace: str, key: str) -> None:
        return None


class RenamedStore:
    """This package's own store after `thread_id` becomes `thread`.

    The regression the conformance suite exists for: nothing about it is wrong
    until a caller writes `store.get(thread_id=...)`, which every caller does.
    """

    async def get(self, thread: str, session_key: str) -> str | None:
        return None

    async def put(self, thread: str, session_key: str, acp_session_id: str) -> None:
        return None

    async def delete(self, thread: str, session_key: str) -> None:
        return None


UNUSABLE = [SynchronousStore(), NamespacedStore(), RenamedStore()]


@pytest.mark.parametrize("store", UNUSABLE, ids=lambda store: type(store).__name__)
def test_isinstance_accepts_stores_no_caller_could_use(store: object) -> None:
    """Why `assert_conforms` exists rather than an isinstance check.

    A `runtime_checkable` protocol compares member names, so each of these is an
    `ACPSessionStore` as far as the interpreter is concerned, and each of them
    raises `TypeError` the first time a node calls it.
    """
    assert isinstance(store, ACPSessionStore)


@pytest.mark.parametrize("store", UNUSABLE, ids=lambda store: type(store).__name__)
def test_the_conformance_check_rejects_them(store: object) -> None:
    with pytest.raises(AssertionError):
        assert_conforms(store)


def test_the_interface_is_three_methods_and_no_more() -> None:
    """The store holds session identifiers. History belongs to the agent, and an
    interface with somewhere to put a transcript is an invitation to keep one."""
    declared = {name for name in vars(ACPSessionStore) if not name.startswith("_")}

    assert declared == {"get", "put", "delete"}
