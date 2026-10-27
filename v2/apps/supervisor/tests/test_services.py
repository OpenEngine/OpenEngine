from __future__ import annotations

from openengine.supervisor.processes import services


def test_vcp_is_excluded_by_default() -> None:
    assert services.enabled(vcp=False) == (
        ("cxn", "connection_bus"),
        ("ge", "graph_engine"),
        ("tools", "tool_service"),
    )


def test_vcp_can_be_opted_in() -> None:
    assert services.enabled(vcp=True) == services.SERVICES


def test_vcp_has_a_ready_file_and_a_state_directory_variable() -> None:
    assert services.READY_FILE_VARIABLES["vcp"] == "OE_VCP_READY_FILE"
    assert services.STATE_DIR_VARIABLES["vcp"] == "OE_VCP_STATE_DIR"
