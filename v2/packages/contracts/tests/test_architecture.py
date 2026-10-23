from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from openengine.contracts.reference import NAMES
from openengine.contracts.validate import SERVICES

# architecture.py is a script under tooling/, not part of the package; load it from its file.
_SPEC = importlib.util.spec_from_file_location("architecture", Path(__file__).resolve().parents[1] / "tooling" / "architecture.py")
assert _SPEC and _SPEC.loader
architecture = importlib.util.module_from_spec(_SPEC)
sys.modules["architecture"] = architecture
_SPEC.loader.exec_module(architecture)

DIAGRAM = architecture.diagram()


@pytest.mark.parametrize("service", SERVICES)
def test_every_subject_is_drawn_with_its_service(service: str) -> None:
    short = NAMES[service][1]
    block = DIAGRAM.split(f"    subgraph {short}[", 1)[1].split("\n    end\n", 1)[0]
    for group, addresses in architecture.subjects(service).items():
        if not addresses:
            continue
        prefix = architecture._prefix(addresses)
        assert f"<b>{prefix}</b>" in block
        for address in addresses:
            assert f"<br/>{address.removeprefix(prefix)}" in block, address


@pytest.mark.parametrize("service", SERVICES)
def test_every_table_is_drawn_under_its_service(service: str) -> None:
    short = NAMES[service][1]
    tables = architecture.tables(short)
    assert tables, f"{short} has no tables"
    block = DIAGRAM.split(f'subgraph {short}_db["database {short}"]', 1)[1].split("end", 1)[0]
    for table in tables:
        assert f'{short}_t_{table}[("{table}")]' in block


def test_every_stream_is_drawn() -> None:
    for name, _ in architecture.streams():
        assert f"stream {name}" in DIAGRAM


def test_apps_and_integrations_connect_where_they_should() -> None:
    assert "    cli --> ge_control & ge_query\n" in DIAGRAM
    assert '    cli -- "connections" --> cxn_control & cxn_query\n' in DIAGRAM
    for integration in ("github", "slack"):
        assert f'    {integration} -- "webhooks" --> cxn_ingress\n' in DIAGRAM
        assert f'    {integration} --- |"← API calls"| cxn_egress\n' in DIAGRAM
    assert '    cxn -- "jobs" --> ge_control\n' in DIAGRAM
    assert '    tools_control --- |"← invoke tools"| sessions\n' in DIAGRAM
    assert '    ge -- "tool grants" --> tools_control\n' in DIAGRAM
    assert '    cxn_query --- |"read by tools"| tools\n' in DIAGRAM
    assert '    vcp -- "execute, steer, cancel" --> daemon\n' in DIAGRAM


def test_virtualization_uses_the_contract_isolation_levels() -> None:
    assert architecture.isolation_levels() == ["process", "container", "microvm", "vm"]
    assert "isolation: process · container · microvm · vm" in DIAGRAM
    assert "smolvm" in architecture.providers()


def test_write_adds_changed_paths_to_the_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(architecture, "V2", tmp_path / "v2")
    monkeypatch.setattr(architecture, "OUTPUT", tmp_path / "v2" / "docs" / "architecture.mermaid")
    architecture.OUTPUT.parent.mkdir(parents=True)
    manifest = tmp_path / "changes.json"
    manifest.write_text('["v2/docs/schema/latest_ge.svg"]')
    assert architecture.write(manifest)
    assert json.loads(manifest.read_text()) == [
        "v2/docs/architecture.mermaid", "v2/docs/architecture.svg", "v2/docs/schema/latest_ge.svg",
    ]
    assert architecture.OUTPUT.read_text() == DIAGRAM
    architecture.OUTPUT.with_suffix(".svg").write_text("<svg/>")
    manifest.write_text("[]")
    assert not architecture.write(manifest)
    assert json.loads(manifest.read_text()) == []
