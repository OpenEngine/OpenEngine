"""Graph conditions handle missing outputs and reject Python execution."""

import pytest

from engine.graph_service.expressions import ExpressionError, parse, template


@pytest.mark.parametrize("source,expected", [
    ("outputs.review.verdict == 'changes_needed' && visits.fix < 2", True),
    ("outputs.missing.score > 0", False),
    ("has(outputs.missing) || size(outputs.review.findings) == 2", True),
    ("outputs.review.findings[0]", "bug"),
    ("outputs.review.findings[9]", None),
    ("lower('BUG') in ['bug', 'security']", True),
    ("1 + 2 * 3 - 4 / 2", 5),
    ("5 % 2", 1),
    ("1 / 0", None),
    ("-visits.fix", -1),
    ("null != false", True),
    ("1 <= visits.fix && visits.fix >= 1", True),
])
def test_conditions_and_values(source, expected):
    names = {"outputs": {"review": {"verdict": "changes_needed", "findings": ["bug", "security"]}}, "visits": {"fix": 1}}
    assert parse(source).evaluate(names) == expected


@pytest.mark.parametrize("source", ["", "x", "inputs.__class__", "inputs.get('x')", "size", "1 and 2", "[x for x in inputs]", "'unterminated", "1 +"])
def test_expressions_reject_unsupported_python(source):
    with pytest.raises(ExpressionError):
        parse(source)


def test_templates_render_missing_values_and_preserve_reference_paths():
    value = template("Review ${inputs.tone}: ${json(outputs.review)} ${outputs.missing}")
    assert value.render({"inputs": {"tone": "brief"}, "outputs": {"review": [True]}}) == 'Review brief: [\n  true\n] '
    assert value.expressions[0].references == (("inputs", "tone"),)
    assert value.single is None
    assert template("${true}").render({}) == "true"
    assert template("${1}").render({}) == "1"
    assert template("${outputs.review}").single.evaluate({"outputs": {"review": [1]}}) == [1]
