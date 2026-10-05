"""Graph expressions preserve data semantics and reject executable Python."""

import pytest

from engine.graph_service.expressions import ExpressionError, parse, template


@pytest.mark.parametrize(("source", "expected"), [
    ("true && false", False),
    ("true && 7", 7),
    ("false || 7", 7),
    ("false || null", None),
    ("(!false)", True),
    ("-3", -3),
    ("-null", None),
    ("2 + 3", 5),
    ("'run ' + 3", "run 3"),
    ("7 - 2", 5),
    ("3 * 4", 12),
    ("7 / 2", 3.5),
    ("7 % 3", 1),
    ("1 / 0", None),
    ("null - 1", None),
    ("2 == 2", True),
    ("2 != 3", True),
    ("1 < 2 <= 2", True),
    ("3 > 2 >= 2", True),
    ("null > 0", False),
    ("'x' > 0", False),
    ("2 in [1, 2]", True),
    ("2 in null", False),
    ("size([1, 2])", 2),
    ("size(null)", 0),
    ("size()", 0),
    ("lower('HELLO')", "hello"),
    ("lower()", ""),
    ("has('value')", True),
    ("has(null)", False),
    ("has()", False),
    ("json([1, true])", "[\n  1,\n  true\n]"),
    ("json()", "null"),
    ("'true && false || !null'", "true && false || !null"),
    (r"'it\'s true'", "it's true"),
])
def test_expression_values(source, expected):
    assert parse(source).evaluate({}) == expected


@pytest.mark.parametrize("source", [
    "", "(", "'unterminated", "unknown", "true and false",
    "inputs.__class__", "lower", "inputs.get('secret')",
    "lower(value='x')", "[x for x in inputs]", "2 ** 8",
])
def test_expressions_reject_unsupported_syntax(source):
    with pytest.raises(ExpressionError):
        parse(source)


def test_templates_read_nested_data_and_render_missing_values():
    names = {"outputs": {"review": {"items": ["first", "last"]}}, "inputs": {"ok": True}}
    expression = parse("outputs.review.items[1]")
    assert expression.references == (("outputs", "review", "items"),)
    assert expression.evaluate(names) == "last"
    for source in ("outputs.missing.value", "outputs.review.items[99]", "outputs.review.items['x']"):
        assert parse(source).evaluate(names) is None
    assert template("${outputs.review.items[-1]}").single.evaluate(names) == "last"
    text = template("ok=${inputs.ok}; missing=${outputs.missing}; count=${size(outputs.review.items)}")
    assert text.single is None
    assert len(text.expressions) == 3
    assert text.render(names) == "ok=true; missing=; count=2"
    assert template("${false}").render({}) == "false"
    assert template("${outputs.review.items}").render(names) == '[\n  "first",\n  "last"\n]'
