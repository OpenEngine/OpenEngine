"""Structural checks for the repository-owned scoper prompt eval set."""

import json
from pathlib import Path


EVAL_SET = (
    Path(__file__).parents[1] / "packages" / "scoper" / "evals" / "scoping_cases.json"
)
EXPECTED_DIMENSIONS = {
    "right_sized",
    "component_focused",
    "interface_first",
    "existing_work_aware",
}


def test_scoper_eval_set_is_well_formed_and_covers_every_dimension() -> None:
    eval_set = json.loads(EVAL_SET.read_text())

    assert eval_set["version"] == 1
    assert set(eval_set["dimensions"]) == EXPECTED_DIMENSIONS
    assert len(eval_set["cases"]) >= 5

    case_ids: set[str] = set()
    covered_dimensions: set[str] = set()
    for case in eval_set["cases"]:
        assert case["id"] not in case_ids
        case_ids.add(case["id"])
        assert case["description"]

        inputs = case["input"]
        assert inputs["milestones"]
        assert isinstance(inputs["workorders"], list)
        assert inputs["policy"]["rules"]

        required = set(case["rubric"]["required"])
        assert required
        assert required <= EXPECTED_DIMENSIONS
        assert case["rubric"]["notes"]
        covered_dimensions.update(required)

    assert covered_dimensions == EXPECTED_DIMENSIONS
