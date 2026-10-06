"""The expression language of YAML graphs: what it evaluates, and what it refuses."""

from __future__ import annotations

import pytest

from engine.graph_service.expressions import ExpressionError, as_text, parse, template

NAMES = {
    "instruction": "Fix it",
    "repository": "example/repo",
    "inputs": {"tone": "plain", "count": "3"},
    "outputs": {
        "review": {"verdict": "changes_needed", "findings": [{"tagline": "a"}, {"tagline": "b"}]},
        "spec": "the spec",
        "reviewers": [{"facet": "security"}, {"facet": "bugs"}],
    },
    "visits": {"fix": 1},
    "item": {"facet": {"name": "Security"}, "model": "elevated"},
}


@pytest.mark.parametrize(("source", "expected"), [
    ("outputs.review.verdict == 'changes_needed' && visits.fix < 2", True),
    ("outputs.review.verdict == \"ready\" || visits.fix >= 1", True),
    ("!has(outputs.missing)", True),
    ("visits.fix != 1", False),
    ("visits.fix <= 1 && visits.fix > 0", True),
    ("size(outputs.review.findings) > 1", True),
    ("size(outputs.spec)", 8),
    ("size(outputs.missing)", 0),
    ("size(outputs)", 3),
    ("lower(instruction)", "fix it"),
    ("lower(null)", ""),
    ("has('')", False),
    ("has(outputs.spec)", True),
    ("'security' in ['security', 'bugs']", True),
    ("!('x' in outputs.missing)", True),
    ("'x' in outputs.missing", False),
    ("outputs.reviewers[0].facet", "security"),
    ("outputs.reviewers[-1].facet", "bugs"),
    ("outputs.reviewers[5]", None),
    ("outputs.spec.deeper", None),
    ("visits.fix + 1", 2),
    ("visits.fix - 3", -2),
    ("visits.fix * 4", 4),
    ("visits.fix / 2", 0.5),
    ("7 % 4", 3),
    ("1 / 0", None),
    ("'n=' + visits.fix", "n=1"),
    ("outputs.missing + 1", None),
    ("-visits.fix", -1),
    ("-instruction", None),
    ("visits.never < 2", False),
    ("visits.never >= 0", False),
    ("'a' < 1", False),
    ("true && null", None),
    ("false || 0", 0),
    ("1 < 2 < 3", True),
    ("3 > 2 > 2", False),
    ("item.facet.name", "Security"),
])
def test_expressions_evaluate_over_a_runs_names(source: str, expected: object) -> None:
    assert parse(source).evaluate(NAMES) == expected


def test_json_renders_structured_values() -> None:
    assert parse("json(outputs.reviewers)").evaluate(NAMES).startswith("[\n")
    assert parse("json(null)").evaluate(NAMES) == "null"


@pytest.mark.parametrize(("source", "complaint"), [
    ("", "empty expression"),
    ("outputs.(", "cannot parse"),
    ("__import__('os')", "can be called"),
    ("open('x')", "can be called"),
    ("os.path", "unknown name"),
    ("'x' not in outputs", "use && || !"),
    ("outputs.review.__class__", "cannot start with '_'"),
    ("size", "is a function"),
    ("size(x=1)", "can be called"),
    ("outputs.review()", "can be called"),
    ("visits.fix if true else 0", "use && || !"),
    ("a and b", "use && || !"),
    ("[x for x in outputs]", "use && || !"),
    ("{'a': 1}", "is not allowed"),
    ("'unterminated", "unterminated string"),
    ("visits.fix ** 2", "is not allowed"),
])
def test_anything_outside_the_language_is_refused(source: str, complaint: str) -> None:
    with pytest.raises(ExpressionError, match=complaint):
        parse(source)


def test_operators_inside_strings_are_left_alone() -> None:
    assert parse("'a && b || !c'").evaluate(NAMES) == "a && b || !c"
    assert parse("'it\\'s' == \"it's\"").evaluate(NAMES) is True


def test_references_name_each_path_read() -> None:
    expression = parse("size(outputs.review.findings) > 0 && visits.fix < inputs.count")
    assert set(expression.references) == {
        ("outputs", "review", "findings"), ("visits", "fix"), ("inputs", "count"),
    }


def test_templates_fill_their_holes() -> None:
    filled = template("Review ${item.facet.name} for ${instruction}: ${json(outputs.review.findings)}")
    assert len(filled.expressions) == 3 and filled.single is None
    rendered = filled.render(NAMES)
    assert rendered.startswith("Review Security for Fix it: [") and '"tagline": "b"' in rendered
    assert template("${inputs.tone}").single is not None
    assert template("no holes").render(NAMES) == "no holes"


@pytest.mark.parametrize(("value", "text"), [
    (None, ""), ("x", "x"), (True, "true"), (False, "false"), (3, "3"), (1.5, "1.5"), ([1], "[\n  1\n]"),
])
def test_values_render_as_text(value: object, text: str) -> None:
    assert as_text(value) == text
