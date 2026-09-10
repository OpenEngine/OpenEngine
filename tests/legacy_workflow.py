"""Explicit access to the retained, unregistered v1 workflow for legacy tests."""

from pathlib import Path
from runpy import run_path

from engine.runtime import WorkflowCatalog

workflow = run_path(
    str(Path(__file__).resolve().parents[1] / "workflows" / "_implementation_review.py")
)["workflow"]
catalog = WorkflowCatalog.from_definitions([workflow])
