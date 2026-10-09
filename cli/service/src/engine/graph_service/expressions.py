"""The small expression language of YAML graphs: `${...}` and `when:`.

    outputs.review.verdict == 'changes_needed' && visits.fix < 2
    size(outputs.rerank.findings) > 0
    Review ${item.facet.name}: ${json(outputs.implement)}

Names: `instruction`, `repository`, `inputs.NAME`, `outputs.NODE[.FIELD...]`,
`visits.NODE`, and `item` inside a parallel node. Literals (`'text'`, numbers,
`true`, `false`, `null`), comparisons, `in`, `&& || !`, `+ - * / %`, indexing,
and the functions `size`, `json`, `lower`, `has`.

Parsed with Python's own parser, after translating `&& || !` and the literal
words, and then admitted only if every node in the tree is on a short list --
so there is no way to reach a Python object, call anything else, or loop.
Missing values read as `null`, and comparing `null` with a number is false
rather than an error, so a condition about a node that has not run yet is
simply not met.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

ROOTS = frozenset({"instruction", "repository", "inputs", "outputs", "visits", "item"})
FUNCTIONS = frozenset({"size", "json", "lower", "has"})
_TEMPLATE = re.compile(r"\$\{([^{}]*)\}")
_WORDS = {"true": "True", "false": "False", "null": "None"}

# Public spellings also define the operator nodes admitted by the parser.
OPERATORS = {
    ast.And: "&&", ast.Or: "||", ast.Not: "!", ast.USub: "-",
    ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.Mod: "%",
    ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=",
    ast.Gt: ">", ast.GtE: ">=", ast.In: "in",
}
_ALLOWED = (
    ast.Expression, ast.BoolOp, ast.UnaryOp, ast.BinOp, ast.Compare,
    *OPERATORS, ast.NotIn,
    ast.Name, ast.Attribute, ast.Subscript, ast.Constant, ast.Call, ast.List, ast.Load,
)


class ExpressionError(ValueError):
    pass


@dataclass(frozen=True)
class Expression:
    source: str
    tree: ast.Expression

    @property
    def references(self) -> tuple[tuple[str, ...], ...]:
        """Each name path read, such as `('outputs', 'review', 'verdict')`."""
        found: list[tuple[str, ...]] = []
        for node in ast.walk(self.tree):
            if isinstance(node, (ast.Attribute, ast.Name)) and not _inside_path(node, self.tree):
                path = _path(node)
                if path:
                    found.append(path)
        return tuple(found)

    def evaluate(self, names: Mapping[str, Any]) -> Any:
        return _evaluate(self.tree.body, names)


def parse(source: str) -> Expression:
    text = _translate(source.strip())
    if not text:
        raise ExpressionError("empty expression")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as error:
        raise ExpressionError(f"cannot parse {source!r}: {error.msg}") from None
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ExpressionError(f"{source!r}: {type(node).__name__} is not allowed")
        if isinstance(node, ast.Name) and node.id not in ROOTS and node.id not in FUNCTIONS:
            raise ExpressionError(
                f"{source!r}: unknown name {node.id!r}; use instruction, repository, "
                "inputs, outputs, visits or item"
            )
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS or node.keywords:
                raise ExpressionError(f"{source!r}: only {', '.join(sorted(FUNCTIONS))} can be called")
        if isinstance(node, ast.Name) and node.id in FUNCTIONS and not _is_called(node, tree):
            raise ExpressionError(f"{source!r}: {node.id} is a function")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ExpressionError(f"{source!r}: names cannot start with '_'")
    return Expression(source, tree)


@dataclass(frozen=True)
class Template:
    """Text with `${...}` holes."""

    source: str
    parts: tuple[str | Expression, ...]

    @property
    def expressions(self) -> tuple[Expression, ...]:
        return tuple(part for part in self.parts if isinstance(part, Expression))

    def render(self, names: Mapping[str, Any]) -> str:
        return "".join(
            part if isinstance(part, str) else as_text(part.evaluate(names)) for part in self.parts
        )

    @property
    def single(self) -> Expression | None:
        """The expression, when the whole template is exactly one hole."""
        if len(self.parts) == 1 and isinstance(self.parts[0], Expression):
            return self.parts[0]
        return None


def template(source: str) -> Template:
    parts: list[str | Expression] = []
    position = 0
    for match in _TEMPLATE.finditer(source):
        if match.start() > position:
            parts.append(source[position:match.start()])
        parts.append(parse(match.group(1)))
        position = match.end()
    if position < len(source):
        parts.append(source[position:])
    return Template(source, tuple(parts))


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, indent=2)


def _translate(source: str) -> str:
    """`&&`, `||`, `!` and the literal words, outside string literals."""
    out: list[str] = []
    index, quote = 0, ""
    while index < len(source):
        char = source[index]
        if quote:
            out.append(char)
            if char == "\\" and index + 1 < len(source):
                out.append(source[index + 1])
                index += 1
            elif char == quote:
                quote = ""
        elif char in "'\"":
            quote = char
            out.append(char)
        elif source.startswith(OPERATORS[ast.And], index):
            out.append(" and ")
            index += 1
        elif source.startswith(OPERATORS[ast.Or], index):
            out.append(" or ")
            index += 1
        elif char == OPERATORS[ast.Not] and not source.startswith("!=", index):
            out.append(" not ")
        elif char.isalpha() or char == "_":
            end = index
            while end < len(source) and (source[end].isalnum() or source[end] == "_"):
                end += 1
            word = source[index:end]
            if word in ("and", "or", "not", "is", "lambda", "if", "else", "for"):
                raise ExpressionError(f"{source!r}: use && || ! rather than Python's {word!r}")
            out.append(_WORDS.get(word, word))
            index = end
            continue
        else:
            out.append(char)
        index += 1
    if quote:
        raise ExpressionError(f"{source!r}: unterminated string")
    return "".join(out)


def _path(node: ast.AST) -> tuple[str, ...]:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name) and node.id in ROOTS:
        return (node.id, *reversed(parts))
    return ()


def _inside_path(node: ast.AST, tree: ast.AST) -> bool:
    """Whether `node` is the inner part of a longer attribute chain."""
    for parent in ast.walk(tree):
        if isinstance(parent, ast.Attribute) and parent.value is node:
            return True
    return False


def _is_called(node: ast.Name, tree: ast.AST) -> bool:
    return any(isinstance(parent, ast.Call) and parent.func is node for parent in ast.walk(tree))


def _evaluate(node: ast.AST, names: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, ast.Attribute):
        return _get(_evaluate(node.value, names), node.attr)
    if isinstance(node, ast.Subscript):
        return _get(_evaluate(node.value, names), _evaluate(node.slice, names))
    if isinstance(node, ast.List):
        return [_evaluate(item, names) for item in node.elts]
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result: Any = True
            for value in node.values:
                result = _evaluate(value, names)
                if not result:
                    return result
            return result
        result = False
        for value in node.values:
            result = _evaluate(value, names)
            if result:
                return result
        return result
    if isinstance(node, ast.UnaryOp):
        operand = _evaluate(node.operand, names)
        if isinstance(node.op, ast.Not):
            return not operand
        return -operand if isinstance(operand, (int, float)) else None
    if isinstance(node, ast.BinOp):
        left, right = _evaluate(node.left, names), _evaluate(node.right, names)
        try:
            if isinstance(node.op, ast.Add):
                if isinstance(left, str) or isinstance(right, str):
                    return as_text(left) + as_text(right)
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            return left % right
        except (TypeError, ZeroDivisionError):
            return None
    if isinstance(node, ast.Compare):
        left = _evaluate(node.left, names)
        for operator, comparator in zip(node.ops, node.comparators):
            right = _evaluate(comparator, names)
            if not _compare(operator, left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.Call):
        assert isinstance(node.func, ast.Name)
        arguments = [_evaluate(argument, names) for argument in node.args]
        return _call(node.func.id, arguments)
    raise ExpressionError(f"cannot evaluate {type(node).__name__}")


def _get(value: Any, key: Any) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    if isinstance(value, (list, tuple)) and isinstance(key, int) and -len(value) <= key < len(value):
        return value[key]
    return None


def _compare(operator: ast.cmpop, left: Any, right: Any) -> bool:
    try:
        if isinstance(operator, ast.Eq):
            return left == right
        if isinstance(operator, ast.NotEq):
            return left != right
        if isinstance(operator, ast.In):
            return right is not None and left in right
        if isinstance(operator, ast.NotIn):
            return right is None or left not in right
        if left is None or right is None:
            return False
        if isinstance(operator, ast.Lt):
            return left < right
        if isinstance(operator, ast.LtE):
            return left <= right
        if isinstance(operator, ast.Gt):
            return left > right
        return left >= right
    except TypeError:
        return False


def _call(name: str, arguments: list[Any]) -> Any:
    if name == "size":
        value = arguments[0] if arguments else None
        return len(value) if isinstance(value, (str, list, tuple, Mapping)) else 0
    if name == "json":
        return json.dumps(arguments[0] if arguments else None, indent=2)
    if name == "lower":
        return as_text(arguments[0] if arguments else None).lower()
    if name == "has":
        return bool(arguments) and arguments[0] not in (None, "", [], {})
    raise ExpressionError(f"unknown function {name}")


__all__ = ["Expression", "ExpressionError", "Template", "as_text", "parse", "template"]


def specification() -> str:
    """Render the public expression syntax from the parser's admission rules."""
    def code(values: Iterable[str]) -> str:
        return ", ".join(f"`{value}`" for value in values)

    lines = [
        "Templates use `${...}`. Expression roots: " + code(sorted(ROOTS)) + ".",
        "Operators: " + code(dict.fromkeys(OPERATORS.values())) + ".",
        "Functions: " + code(f"{name}(...)" for name in sorted(FUNCTIONS)) + ".",
        "Literal words: " + code(_WORDS) + "; text and numeric literals are supported.",
    ]
    for node, example in ((ast.Attribute, "outputs.STEP.field"),
                          (ast.Subscript, "outputs.STEP.items[0]"),
                          (ast.List, "[1, 2]")):
        if node in _ALLOWED:
            parse(example)
            lines.append(f"{node.__name__}: `{example}`.")
    lines.append(
        "Express \"not in\" as `(!(item in inputs.items))`; Python's `not` keyword is rejected. "
        "Expressions cannot execute Python. Undeclared inputs and unavailable outputs "
        "are reported at registration."
    )
    return "\n".join(lines)
