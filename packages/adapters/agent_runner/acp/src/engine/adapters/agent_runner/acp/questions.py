"""An ACP form elicitation, read as the port's questions and answered from them.

Both adapters put a question to the client as `elicitation/create` with a flat
JSON schema: one property per question, an option list as `oneOf`/`anyOf`
entries or an `enum`, and a multi-select as an array of them. Each also pairs a
question with a free-text property for an answer that is none of the options,
tagged in the property's `_meta` with the `questionId` it belongs to --
`_askUserQuestionCustomAnswer` from claude-agent-acp, `codex` with the role
`user_note` from codex-acp. That property is folded into its question here
rather than asked separately, and an answer that is not one of the options is
written back into it.

Codex titles a property with the question and describes it with the header;
Claude does the reverse. Which one sent the form is read off the same `_meta`.
"""

from collections.abc import Mapping
from typing import Any

from engine.ports.agent_runner import (
    UserInputOption,
    UserInputQuestion,
    UserInputResponse,
)


def questions_from_form(
    message: str, schema: Mapping[str, Any]
) -> tuple[UserInputQuestion, ...]:
    properties = _properties(schema)
    others = _other_fields(properties)
    asked = [key for key in properties if key not in others]
    questions: list[UserInputQuestion] = []
    for key in asked:
        prop = properties[key]
        title = _text(prop.get("title"))
        description = _text(prop.get("description"))
        if _is_codex(prop):
            title, description = description, title
        options = _options(prop)
        questions.append(
            UserInputQuestion(
                question_id=key,
                header=title or key,
                question=description
                or (message if len(asked) == 1 and message else "")
                or title
                or key,
                options=tuple(
                    UserInputOption(label=label, description=detail)
                    for label, detail in options
                ),
                multi_select=prop.get("type") == "array",
                allows_other=not options or key in others.values(),
            )
        )
    return tuple(questions)


def content_from_answers(
    schema: Mapping[str, Any], response: UserInputResponse
) -> dict[str, Any]:
    """The form content that says what `response` says."""
    properties = _properties(schema)
    other_of = {question: field for field, question in _other_fields(properties).items()}
    content: dict[str, Any] = {}
    for answer in response.answers:
        prop = properties.get(answer.question_id)
        values = [value for value in answer.answers if value.strip()]
        if prop is None or not values:
            continue
        labels = {label for label, _detail in _options(prop)}
        picks = [value for value in values if value in labels] if labels else values
        typed = [value for value in values if value not in picks]
        other = other_of.get(answer.question_id)
        if typed and other is None:
            # Nowhere to write text the options do not cover: send it anyway and
            # let the agent say what it makes of it.
            picks, typed = values, []
        if typed:
            content[other] = ", ".join(typed)
        if not picks:
            continue
        if prop.get("type") == "array":
            content[answer.question_id] = picks
        else:
            content[answer.question_id] = _typed(prop.get("type"), ", ".join(picks))
    return content


def _properties(schema: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return {}
    return {
        str(key): value for key, value in properties.items() if isinstance(value, Mapping)
    }


def _other_fields(properties: Mapping[str, Mapping[str, Any]]) -> dict[str, str]:
    """Free-text properties that belong to another question, and which one."""
    found: dict[str, str] = {}
    for key, prop in properties.items():
        meta = prop.get("_meta")
        if not isinstance(meta, Mapping):
            continue
        for tag in meta.values():
            if not isinstance(tag, Mapping):
                continue
            question = tag.get("questionId")
            if (
                isinstance(question, str)
                and question in properties
                and question != key
                and (tag.get("isCustomAnswer") is True or tag.get("role") == "user_note")
            ):
                found[key] = question
    return found


def _is_codex(prop: Mapping[str, Any]) -> bool:
    meta = prop.get("_meta")
    return isinstance(meta, Mapping) and isinstance(meta.get("codex"), Mapping)


def _options(prop: Mapping[str, Any]) -> list[tuple[str, str]]:
    source = prop.get("items") if prop.get("type") == "array" else prop
    if not isinstance(source, Mapping):
        return []
    for listed in ("oneOf", "anyOf"):
        entries = source.get(listed)
        if isinstance(entries, list):
            return [
                (str(entry["const"]), _text(entry.get("description")))
                for entry in entries
                if isinstance(entry, Mapping) and entry.get("const") is not None
            ]
    enum = source.get("enum")
    if isinstance(enum, list):
        return [(str(value), "") for value in enum]
    return []


def _typed(kind: object, value: str) -> Any:
    """A free-text answer as the primitive the schema asked for."""
    try:
        match kind:
            case "boolean":
                return value.strip().lower() in ("true", "yes", "y", "1")
            case "integer":
                return int(value)
            case "number":
                return float(value)
    except ValueError:
        pass
    return value


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


__all__ = ["content_from_answers", "questions_from_form"]
