"""The syntax of Graph expressions: `${...}` in templates, and `when:` and `choose:`.

    outputs.review.verdict == 'changes_needed' && visits.fix < 2
    size(outputs.rerank.findings) > 0
    Review ${item.facet.name}: ${json(outputs.implement)}

Ported from V1's `graph_service/expressions.py`, so both read one language.
Names: `instruction`, `repository`, `inputs.NAME`, `outputs.STEP[.FIELD...]`,
`visits.STEP`, and `item` inside a faceted step. Literals (`'text'`, numbers,
`true`, `false`, `null`), comparisons, `in`, `&& || !`, `+ - * / %`, indexing,
and the functions `size`, `json`, `lower`, `has`.

An expression is parsed with Python's own parser, after translating `&& || !`
and the literal words, and admitted only if every node in the tree is on a
short list. This module only checks syntax and reports the names an
expression reads; it never evaluates one. The Graph Engine does that when a
step runs.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from dataclasses import dataclass

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
    def __init__(self, source: str, problem: str) -> None:
        super().__init__(f"{source!r}: {problem}" if source.strip() else problem)


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


def parse(source: str) -> Expression:
    text = _translate(source.strip()).strip()
    if not text:
        raise ExpressionError(source, "empty expression")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as error:
        raise ExpressionError(source, f"cannot parse: {error.msg}") from None
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ExpressionError(source, f"{type(node).__name__} is not allowed")
        if isinstance(node, ast.Name) and node.id not in ROOTS and node.id not in FUNCTIONS:
            raise ExpressionError(
                source, f"unknown name {node.id!r}; use instruction, repository, "
                "inputs, outputs, visits or item"
            )
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS or node.keywords:
                raise ExpressionError(source, f"only {', '.join(sorted(FUNCTIONS))} can be called")
        if isinstance(node, ast.Name) and node.id in FUNCTIONS and not _is_called(node, tree):
            raise ExpressionError(source, f"{node.id} is a function")
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ExpressionError(source, "names cannot start with '_'")
    return Expression(source, tree)


def holes(source: str) -> tuple[str, ...]:
    """The source of each `${...}` hole of a template, in order, unparsed."""
    return tuple(match.group(1) for match in _TEMPLATE.finditer(source))


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
                raise ExpressionError(source, f"use && || ! rather than Python's {word!r}")
            out.append(_WORDS.get(word, word))
            index = end
            continue
        else:
            out.append(char)
        index += 1
    if quote:
        raise ExpressionError(source, "unterminated string")
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
    return any(isinstance(parent, ast.Attribute) and parent.value is node for parent in ast.walk(tree))


def _is_called(node: ast.Name, tree: ast.AST) -> bool:
    return any(isinstance(parent, ast.Call) and parent.func is node for parent in ast.walk(tree))


def markdown() -> str:
    """The expression syntax as a Markdown section, from the parser's admission rules."""
    def code(values: Iterable[str]) -> str:
        return ", ".join(f"`{value}`" for value in values)

    examples = (
        "outputs.ci.passed == false && visits.implement < 4",
        "size(outputs.consolidate.findings) > 0",
        "outputs.approve.decision in ['approved', 'merged']",
        "!(has(inputs.base))",
        "outputs.reviewers[0].findings",
    )
    for example in examples:
        parse(example)
    return "\n".join([
        "## Expression syntax",
        "",
        "Templates, such as a step's `prompt`, fill in each `${...}` when the step runs. "
        "`when` and `choose` are bare expressions, without `${...}`.",
        "",
        "- Roots: " + code(sorted(ROOTS)) + ". `inputs.NAME` must be a declared input, "
        "`outputs.STEP` and `visits.STEP` a declared step, and `item` is only defined "
        "in a step with `facets`.",
        "- Operators: " + code(dict.fromkeys(OPERATORS.values())) + ", and indexing such as `[0]`.",
        "- Functions: " + code(f"{name}(...)" for name in sorted(FUNCTIONS)) + ".",
        "- Literals: " + code(_WORDS) + ", `'text'`, numbers and lists such as `[1, 2]`.",
        "- Write \"not in\" as `!(x in y)`; Python's `and`, `or`, `not`, `is` and `if` are rejected, "
        "and nothing else can be called.",
        "",
        "Examples:",
        "",
        *(f"- `{example}`" for example in examples),
        "",
        "`oe graph validate` checks the syntax and the names an expression reads. It never "
        "evaluates one; a missing value reads as `null` when the step runs.",
    ]) + "\n"
