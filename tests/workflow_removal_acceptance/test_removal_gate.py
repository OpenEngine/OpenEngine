"""Static acceptance gate for deleting the hardcoded workflow module."""

import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]
pytestmark = pytest.mark.workflow_removal_acceptance
LEGACY_MODULE = "engine.core.workflows.implementation_review"
LEGACY_FILE = (
    ROOT
    / "packages"
    / "engine"
    / "src"
    / "engine"
    / "core"
    / "workflows"
    / "implementation_review.py"
)
LEGACY_PACKAGE = LEGACY_FILE.parent
WORKFLOW_SPECIFIC_TOKENS = (
    "implementation_review",
    "IMPLEMENTATION_REVIEW_WORKFLOW_ID",
    "IMPLEMENTATION_STEP",
    "REVIEW_STEP",
    "HUMAN_REVIEW_STEP",
    "IMPLEMENTATION_PROFILE",
    "REVIEW_PROFILE",
    "WORKFLOW_NAME_PROMPT",
    "WORKFLOW_NAMING_PROFILE",
    "WORKFLOW_DECIDERS",
    "decide_implementation_review",
    "start_implementation_command",
    "start_review_command",
    "implementation-review-v1",
    "RunPhase.IMPLEMENTING",
    "RunPhase.REVIEWING",
)


def _imports_legacy_module(node: ast.Import | ast.ImportFrom) -> bool:
    if isinstance(node, ast.Import):
        return any(
            alias.name == LEGACY_MODULE
            or alias.name.startswith(f"{LEGACY_MODULE}.")
            for alias in node.names
        )
    if node.module == LEGACY_MODULE or (
        node.module is not None and node.module.startswith(f"{LEGACY_MODULE}.")
    ):
        return True
    return node.module == "engine.core.workflows" and any(
        alias.name == "implementation_review" for alias in node.names
    )


def _legacy_imports() -> list[str]:
    violations: list[str] = []
    for source_root in (
        ROOT / "packages",
        ROOT / "apps",
        ROOT / "tests",
        ROOT / "workflows",
    ):
        for path in source_root.rglob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)) and (
                    _imports_legacy_module(node)
                ):
                    violations.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return sorted(violations)


#: Tests that live under an application rather than in `tests/`. The gate is
#: about *production* naming one workflow; a test necessarily names the workflow
#: it drives, and the browser tier's fixtures are tests wherever they sit.
TEST_DIRECTORIES = (ROOT / "apps" / "web" / "e2e",)


def _workflow_specific_production_references() -> list[str]:
    violations: list[str] = []
    for source_root in (ROOT / "packages", ROOT / "apps"):
        for path in source_root.rglob("*"):
            if path.suffix not in {".py", ".ts", ".tsx"}:
                continue
            if any(path.is_relative_to(directory) for directory in TEST_DIRECTORIES):
                continue
            for line_number, line in enumerate(path.read_text().splitlines(), start=1):
                if any(token in line for token in WORKFLOW_SPECIFIC_TOKENS):
                    violations.append(f"{path.relative_to(ROOT)}:{line_number}")
    return sorted(violations)


def test_acceptance_hardcoded_workflow_module_and_imports_are_gone() -> None:
    assert not LEGACY_FILE.exists(), LEGACY_FILE.relative_to(ROOT)
    assert not LEGACY_PACKAGE.exists(), LEGACY_PACKAGE.relative_to(ROOT)
    assert _legacy_imports() == []


def test_acceptance_production_has_no_implementation_review_special_cases() -> None:
    assert _workflow_specific_production_references() == []
