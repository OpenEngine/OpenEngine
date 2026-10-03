"""Styled terminal text measured in cells rather than characters.

A line is a list of `(style, text)` segments. Styles are SGR parameter strings
(`"1"`, `"2;36"`), and the empty style is the terminal's default. Keeping lines
as segments until the last moment is what lets a pane be clipped to its width
without cutting an escape sequence in half.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Sequence

Segment = tuple[str, str]
Line = list[Segment]

BOLD = "1"
DIM = "2"
ITALIC = "3"
REVERSE = "7"
RED = "31"
GREEN = "32"
YELLOW = "33"
BLUE = "34"
MAGENTA = "35"
CYAN = "36"
SELECTED = "7"


def char_width(char: str) -> int:
    """How many cells a character takes: 0, 1, or 2."""
    if not char or unicodedata.combining(char) or char in "​‍️":
        return 0
    code = ord(char)
    if code < 32 or 0x7F <= code < 0xA0:
        return 0
    if unicodedata.east_asian_width(char) in ("W", "F") or 0x1F300 <= code <= 0x1FAFF:
        return 2
    return 1


def text_width(text: str) -> int:
    return sum(char_width(char) for char in text)


def line_width(line: Sequence[Segment]) -> int:
    return sum(text_width(text) for _style, text in line)


def clean(text: str) -> str:
    """Drop what would move the cursor or restyle the screen if printed."""
    return "".join(
        " " if char == "\t" else char
        for char in text
        if char == "\t" or char_width(char) or unicodedata.combining(char)
        or char in "‍️"
    )


def clip(line: Sequence[Segment], width: int, *, fill: str = "") -> Line:
    """The line cut to `width` cells, ending in an ellipsis when it was cut.

    Padded with spaces in `fill`'s style when shorter, so a pane always owns
    every cell of its rows and a previous frame never shows through.
    """
    if width <= 0:
        return []
    total = line_width(line)
    result: Line = []
    if total <= width:
        result = [(style, text) for style, text in line if text]
        if total < width:
            result.append((fill, " " * (width - total)))
        return result
    budget = width - 1
    for style, text in line:
        taken = []
        for char in text:
            cells = char_width(char)
            if cells > budget:
                break
            taken.append(char)
            budget -= cells
        if taken:
            result.append((style, "".join(taken)))
        if budget <= 0 or len(taken) < len(text):
            break
    last_style = result[-1][0] if result else ""
    result.append((last_style, "…"))
    used = line_width(result)
    if used < width:
        result.append((fill, " " * (width - used)))
    return result


def styled(line: Sequence[Segment], base: str = "") -> str:
    """ANSI for one line. `base` is layered under every segment's own style."""
    out = []
    for style, text in line:
        codes = ";".join(code for code in (base, style) if code)
        out.append(f"\x1b[0;{codes}m{text}" if codes else f"\x1b[0m{text}")
    out.append("\x1b[0m")
    return "".join(out)


def wrap(text: str, width: int) -> list[str]:
    """Word-wrap to `width` cells, keeping the text's own line breaks."""
    width = max(width, 1)
    lines: list[str] = []
    for paragraph in clean_lines(text):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        current_width = 0
        for word in _words(paragraph):
            size = text_width(word)
            if current_width + size <= width:
                current += word
                current_width += size
                continue
            if current.strip():
                lines.append(current.rstrip())
            current, current_width = "", 0
            word = word.lstrip() if not current else word
            size = text_width(word)
            while size > width:
                head, word = _split_at(word, width)
                lines.append(head)
                size = text_width(word)
            current, current_width = word, size
        lines.append(current.rstrip())
    return lines


def clean_lines(text: str) -> list[str]:
    return [clean(line) for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]


def _words(text: str) -> Iterable[str]:
    word = ""
    for char in text:
        if char == " " and word and not word.endswith(" "):
            yield word
            word = char
        else:
            word += char
    if word:
        yield word


def _split_at(text: str, width: int) -> tuple[str, str]:
    used = 0
    for index, char in enumerate(text):
        cells = char_width(char)
        if used + cells > width:
            return text[:index], text[index:]
        used += cells
    return text, ""


def plain(line: Sequence[Segment]) -> str:
    return "".join(text for _style, text in line)
