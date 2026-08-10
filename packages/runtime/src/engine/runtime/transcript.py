"""Flattening a conversation into one block of text.

CLI-shaped agents take a prompt, not a message array, so every such adapter has
to turn our conversation into text. That is identical work for all of them, and
two copies of it would drift into two subtly different prompts -- so it lives
here, in the runtime, where an adapter may import it and no vendor is named.

**The output is append-only, and that is load-bearing.** Every message renders
to a fixed block and blocks are joined in order, so the text for turn N is a
strict prefix of the text for turn N+1. Prompt caches match on prefixes. An
earlier version of this moved the latest message between two headings on every
turn, which broke the shared prefix one line in and made a cache hit impossible.

Nothing may be inserted, reordered, or reworded after the fact -- including a
trailing "now answer this", which is why there isn't one. A conversation ending
on a user message is cue enough.
"""

from collections.abc import Mapping, Sequence

from engine.domain.chat import Message, Role

#: How each role is labelled once the structure a chat API carries in roles has
#: to be spelled out in plain text.
ROLE_LABELS: Mapping[Role, str] = {
    Role.SYSTEM: "System",
    Role.USER: "User",
    Role.ASSISTANT: "Assistant",
    Role.TOOL: "Tool result",
}

#: How much of a past step's output is replayed into a later prompt. The full
#: text is always stored; this only bounds what a later turn re-reads, because an
#: 8KB file dump from three questions ago is billed again on every turn after it
#: and rarely earns it.
MAX_REPLAYED_OUTPUT_CHARS = 1000


def render_message(message: Message, max_output_chars: int = MAX_REPLAYED_OUTPUT_CHARS) -> str:
    """One conversation entry as a stable block of text.

    Stable is the operative word: the same message must render identically
    every time it appears, or the append-only property is lost.
    """
    if message.tool_calls:
        return "\n\n".join(
            f"{ROLE_LABELS[message.role]} ran {call.name}: {call.arguments}"
            for call in message.tool_calls
        )
    body = message.content.strip()
    if message.role is Role.TOOL and len(body) > max_output_chars:
        omitted = len(body) - max_output_chars
        body = f"{body[:max_output_chars]}\n… ({omitted} more characters, stored in full)"
    return f"{ROLE_LABELS[message.role]}: {body}"


def flatten(
    messages: Sequence[Message], max_output_chars: int = MAX_REPLAYED_OUTPUT_CHARS
) -> str:
    """A conversation as one labelled, append-only block.

    Messages with neither content nor tool calls are dropped: they would render
    as a bare role label, which reads to a model as an empty turn.
    """
    if not messages:
        raise ValueError("cannot render a conversation with no messages")
    rendered = [
        render_message(message, max_output_chars)
        for message in messages
        if message.content.strip() or message.tool_calls
    ]
    return "# Conversation\n\n" + "\n\n".join(rendered)


__all__ = ["MAX_REPLAYED_OUTPUT_CHARS", "ROLE_LABELS", "flatten", "render_message"]
