"""Scoped proposals survive restart with local references resolved to ticket ids."""

import asyncio
import sqlite3

from alembic import command
import pytest

from engine.adapters.state_store.memory import InMemoryStateStore
from engine.adapters.state_store.sqlite import SQLiteStateStore
from engine.domain import (
    MilestoneId, ScopingPlan, Supersession, TicketApproval, TicketLayer,
    TicketSourceKind, TicketSourceRef, WorkOrderId, WorkOrderSpec,
)
from engine.scoper import _plan
from migrations.migration import alembic_config, upgrade


def spec(key, **kwargs):
    return WorkOrderSpec(MilestoneId("milestone"), key, "Implement " + key,
                         key=key, **kwargs)


@pytest.mark.parametrize("adapter", ["memory", "sqlite"])
def test_plan_round_trip_dependencies_hierarchy_and_approval(tmp_path, adapter):
    async def scenario():
        path = tmp_path / "state.db"
        store = InMemoryStateStore() if adapter == "memory" else SQLiteStateStore(path)
        proposal = ScopingPlan(
            create=(spec("api", layer=TicketLayer.API, estimated_changed_lines=120,
                         acceptance_criteria=("Returns the contract",),
                         dependencies=(WorkOrderId("existing"),),
                         dependency_keys=("contracts",), parent_key="contracts",
                         source_ref=TicketSourceRef(TicketSourceKind.GITHUB_ISSUE,
                                                  "https://github.com/org/repo/issues/1")),),
            supersede=(Supersession(WorkOrderId("old"), (
                spec("contracts", layer=TicketLayer.CONTRACTS),)),),
            cancel=(WorkOrderId("obsolete"),), reasons=("Scope the API",),
        )
        stored = await store.save_scoping_plan("loop", proposal)
        api, contracts = stored.tickets
        assert stored.plan == proposal
        assert api.spec.dependencies == ("existing", contracts.workorder_id)
        assert api.parent_id == contracts.workorder_id
        assert contracts.subtask_ids == (api.workorder_id,)
        assert await store.list_loop_queue("loop") == ()
        approved = await store.set_ticket_approval(
            stored.plan_id, contracts.workorder_id, TicketApproval.APPROVED)
        approved = await store.set_ticket_approval(
            stored.plan_id, api.workorder_id, TicketApproval.APPROVED)
        if adapter == "sqlite":
            store._connection.close()
            store = SQLiteStateStore(path)
        assert await store.load_scoping_plan(stored.plan_id) == approved
        queue = await store.list_loop_queue("loop")
        assert [item.ticket.workorder_id for item in queue] == [
            api.workorder_id, contracts.workorder_id]
        assert all(item.plan_id == stored.plan_id and item.loop_id == "loop"
                   for item in queue)
        assert queue[0].ticket.spec.dependencies == ("existing", contracts.workorder_id)
        assert await store.list_loop_queue("other") == ()
        rejected = await store.set_ticket_approval(
            stored.plan_id, api.workorder_id, TicketApproval.REJECTED)
        assert len(await store.list_loop_queue("loop")) == 1
        assert await store.load_scoping_plan(stored.plan_id) == rejected
        assert await store.load_scoping_plan("missing") is None
        with pytest.raises(KeyError):
            await store.set_ticket_approval(stored.plan_id, "missing", TicketApproval.APPROVED)
        with pytest.raises(KeyError):
            await store.set_ticket_approval("missing", api.workorder_id, TicketApproval.APPROVED)
        # Local names belong to a plan, never to the loop or database.
        second = await store.save_scoping_plan("loop", proposal)
        assert second.tickets[0].spec.dependencies[-1] == second.tickets[1].workorder_id
        assert second.tickets[1].workorder_id != contracts.workorder_id
    asyncio.run(scenario())


@pytest.mark.parametrize("adapter", ["memory", "sqlite"])
@pytest.mark.parametrize("tickets", [
    (spec("same"), spec("same")),
    (spec("one", dependency_keys=("missing",)),),
    (spec("one", parent_key="missing"),),
    (spec("one", dependency_keys=("one",)),),
    (spec("one", dependency_keys=("two",)), spec("two", dependency_keys=("one",))),
    (spec("one", parent_key="two"), spec("two", parent_key="one")),
])
def test_invalid_references_do_not_persist_a_partial_plan(tmp_path, adapter, tickets):
    async def scenario():
        store = InMemoryStateStore() if adapter == "memory" else SQLiteStateStore(tmp_path / "db")
        with pytest.raises(ValueError):
            await store.save_scoping_plan("loop", ScopingPlan(create=tickets))
        assert await store.list_loop_queue("loop") == ()
        if adapter == "sqlite":
            assert store._connection.execute("SELECT count(*) FROM scoping_plans").fetchone()[0] == 0
        else:
            assert not store._scoping_plans
    asyncio.run(scenario())


@pytest.mark.parametrize("changes", [
    {"estimated_changed_lines": -1}, {"estimated_changed_lines": True},
    {"estimated_changed_lines": 1.5}, {"layer": "unknown"},
    {"approval": "pending"}, {"acceptance_criteria": ("",)},
    {"dependency_keys": "other"}, {"parent_key": ""},
])
def test_invalid_ticket_contract(changes):
    with pytest.raises(ValueError):
        spec("ticket", **changes)


@pytest.mark.parametrize("kind", list(TicketSourceKind))
def test_source_refs(kind):
    assert TicketSourceRef(kind.value, "TEAM-12").kind is kind
    with pytest.raises(ValueError):
        TicketSourceRef(kind, "")


def test_scoper_preserves_contract_fields_but_cannot_approve():
    import json
    plan = _plan(json.dumps({"create": [{
        "milestone_id": "m", "name": "API", "objective": "Ship API", "key": "api",
        "layer": "api", "estimated_changed_lines": 42,
        "acceptance_criteria": ["Returns 200"], "dependency_keys": ["data"],
        "parent_key": "feature", "approval": "approved",
        "source_ref": {"kind": "jira_issue", "ref": "TEAM-12"},
    }]}))
    ticket = plan.create[0]
    assert ticket.layer is TicketLayer.API
    assert ticket.estimated_changed_lines == 42
    assert ticket.acceptance_criteria == ("Returns 200",)
    assert ticket.dependency_keys == ("data",)
    assert ticket.parent_key == "feature"
    assert ticket.source_ref == TicketSourceRef(TicketSourceKind.JIRA_ISSUE, "TEAM-12")
    assert ticket.approval is TicketApproval.PROPOSED


def test_scoping_migration_up_down_and_up(tmp_path):
    path = tmp_path / "migration.db"
    url = f"sqlite:///{path}"
    upgrade(url, "d487944fbd15")
    upgrade(url)
    with sqlite3.connect(path) as connection:
        connection.execute("INSERT INTO scoping_plans (plan_id, loop_id, plan_json) VALUES ('p', 'l', '{}')")
        assert connection.execute("SELECT plan_id FROM scoping_plans").fetchone() == ("p",)
        assert connection.execute("PRAGMA index_list(scoping_plans)").fetchall()
    command.downgrade(alembic_config(url), "d487944fbd15")
    with sqlite3.connect(path) as connection:
        assert not connection.execute("SELECT name FROM sqlite_master WHERE name = 'scoping_plans'").fetchall()
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("d487944fbd15",)
    upgrade(url)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM scoping_plans").fetchone() == (0,)
