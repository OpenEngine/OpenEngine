from __future__ import annotations

from openengine.cli.output.render import table


def test_aligns_columns_to_the_widest_cell() -> None:
    assert table(["NAME", "PID"], [["supervisor", "1"], ["nats", "12345"]]) == (
        "NAME        PID\nsupervisor  1\nnats        12345"
    )
