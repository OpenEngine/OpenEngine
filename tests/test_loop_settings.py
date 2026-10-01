"""The settings the rail's Loops section reads and saves."""

import json

import pytest
from starlette.testclient import TestClient

from engine.adapters.state_store.sqlite import SQLiteStateStore
from engine.apps.web.api import create_app
from engine.apps.web.loops import LoopSettings, LoopSettingsStore, parse_loop_settings
from engine.runtime import AgentSession, Capabilities

_SETTINGS = {
    "activeHours": {"start": "09:00", "end": "17:30"},
    "maxPrs": 2,
    "maxDailySpend": 12.5,
    "runnerStrategy": "manual",
    "implementationRunner": "codex",
    "reviewRunner": "claude",
}


def _app(tmp_path, store: LoopSettingsStore):
    stub = object()
    capabilities = Capabilities(
        workflow_runtime=stub,
        source_control=stub,
        agent_runner=stub,
        communications=stub,
        workspace_provider=stub,
        state_store=SQLiteStateStore(str(tmp_path / "t.sqlite3")),
    )
    runners = {"codex": stub, "claude": stub}
    session = AgentSession(capabilities, profiles={}, runners=runners)
    return create_app(session, runners, loop_settings=store)


def test_saved_settings_are_read_back(tmp_path) -> None:
    store = LoopSettingsStore(tmp_path / "loops.json")
    with TestClient(_app(tmp_path, store)) as client:
        before = client.get("/api/loops/settings").json()
        saved = client.put("/api/loops/settings", json=_SETTINGS)
        after = client.get("/api/loops/settings").json()

    assert before == LoopSettings().json()
    assert saved.status_code == 200
    assert saved.json() == after == _SETTINGS
    assert LoopSettingsStore(tmp_path / "loops.json").get().max_prs == 2


def test_rejected_settings_leave_the_saved_ones(tmp_path) -> None:
    store = LoopSettingsStore(tmp_path / "loops.json")
    with TestClient(_app(tmp_path, store)) as client:
        rejected = client.put(
            "/api/loops/settings", json={**_SETTINGS, "reviewRunner": "unknown"}
        )
        after = client.get("/api/loops/settings").json()

    assert rejected.status_code == 400
    assert after == LoopSettings().json()


def test_a_spend_limit_that_is_not_a_number_is_refused(tmp_path) -> None:
    store = LoopSettingsStore(tmp_path / "loops.json")
    body = json.dumps({**_SETTINGS, "maxDailySpend": 0}).replace(
        '"maxDailySpend": 0', '"maxDailySpend": NaN'
    )
    with TestClient(_app(tmp_path, store)) as client:
        rejected = client.put(
            "/api/loops/settings", content=body, headers={"content-type": "application/json"}
        )
        after = client.get("/api/loops/settings")

    assert rejected.status_code == 400
    assert after.status_code == 200


def test_only_manual_keeps_its_runners() -> None:
    settings = parse_loop_settings({**_SETTINGS, "runnerStrategy": "round-robin"}, ["codex"])

    assert settings.implementation_runner == settings.review_runner == ""


@pytest.mark.parametrize("change", [
    {"activeHours": {"start": "9am", "end": "17:00"}},
    {"maxPrs": 0},
    {"maxPrs": True},
    {"maxDailySpend": -1},
    {"maxDailySpend": float("nan")},
    {"maxDailySpend": float("inf")},
    {"runnerStrategy": "random"},
])
def test_invalid_settings_are_refused(change) -> None:
    with pytest.raises(ValueError):
        parse_loop_settings({**_SETTINGS, **change}, ["codex", "claude"])
