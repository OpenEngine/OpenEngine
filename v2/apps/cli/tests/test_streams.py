from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

import pytest

from openengine.cli.migrations.streams import (
    InvalidStreamConfig,
    MigrationError,
    drifted,
    load_stream_configs,
    migrate_streams,
    streams_directory,
)


def test_packages_the_infra_stream_configs() -> None:
    names = [config["name"] for config in load_stream_configs(streams_directory())]

    assert names == ["cxn-egress", "cxn-events", "cxn-ingress", "ge-events", "tools-events", "vcp-events"]


def test_refuses_a_config_named_differently_from_its_file(tmp_path: Path) -> None:
    (tmp_path / "orders.json").write_text(json.dumps({"name": "payments", "subjects": ["orders.>"]}))

    with pytest.raises(InvalidStreamConfig, match='"name" is "orders"'):
        load_stream_configs(tmp_path)


def test_ignores_fields_the_config_leaves_to_the_server_defaults() -> None:
    desired = {"name": "orders", "subjects": ["orders.>"]}

    assert not drifted(desired, {**desired, "max_msgs": -1, "sealed": False})
    assert drifted(desired, {**desired, "subjects": ["orders.new"]})


def test_reports_an_unreachable_server_instead_of_retrying_forever() -> None:
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]

    with pytest.raises(MigrationError, match=f"could not connect to NATS at nats://127.0.0.1:{port}"):
        asyncio.run(asyncio.wait_for(migrate_streams(f"nats://127.0.0.1:{port}", []), timeout=10))
