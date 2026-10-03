"""A multi-line text field with the editing keys people already have in their hands.

| Keys                                   | Does                                   |
| -------------------------------------- | -------------------------------------- |
| Option/Alt+Left/Right, Ctrl+Left/Right, ESC b / ESC f | move by word            |
| Cmd+Left/Right, Home/End, Ctrl+A/E     | start / end of line                    |
| Cmd+Up/Down                            | start / end of the text                |
| Up/Down                                | previous / next line                   |
| Option/Alt+Backspace, Ctrl+W, Ctrl+Backspace | delete the word before the cursor |
| Option/Alt+Delete, Alt+D               | delete the word after the cursor       |
| Cmd+Backspace, Ctrl+U                  | delete everything before the cursor    |
| Ctrl+K                                 | delete to the end of the line          |

Cmd+Backspace reaches a terminal program as Ctrl+U in macOS terminals that do
natural text editing, and as a Super-modified Backspace under the kitty
protocol; both clear from the cursor back to the start of the text.
"""

from __future__ import annotations

from engine.apps.cli.tui.keys import Key
from engine.apps.cli.tui.text import text_width


class TextBuffer:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.cursor = len(text)
        self._column: int | None = None

    def __bool__(self) -> bool:
        return bool(self.text.strip())

    def set(self, text: str) -> None:
        self.text = text
        self.cursor = len(text)
        self._column = None

    def clear(self) -> None:
        self.set("")

    def handle(self, key: Key) -> bool:
        """Apply one key. False when the key is not an editing key."""
        vertical = key.name in ("up", "down")
        handled = self._handle(key)
        if handled and not vertical:
            self._column = None
        return handled

    def _handle(self, key: Key) -> bool:
        name = key.name
        if name == "paste":
            self.insert(key.text.replace("\r\n", "\n").replace("\r", "\n"))
            return True
        if name == "newline":
            self.insert("\n")
            return True
        if name == "char":
            if key.ctrl:
                return self._control(key.text)
            if key.alt:
                return self._meta(key.text)
            if key.super:
                return False
            self.insert(key.text)
            return True
        if name == "backspace":
            if key.super:
                self._delete(0, self.cursor)
            elif key.alt or key.ctrl:
                self._delete(self._word_left(), self.cursor)
            elif self.cursor:
                self._delete(self.cursor - 1, self.cursor)
            return True
        if name == "delete":
            if key.alt or key.ctrl:
                self._delete(self.cursor, self._word_right())
            elif key.super:
                self._delete(self.cursor, self._line_end())
            else:
                self._delete(self.cursor, min(self.cursor + 1, len(self.text)))
            return True
        if name in ("left", "right"):
            forward = name == "right"
            if key.super:
                self.cursor = self._line_end() if forward else self._line_start()
            elif key.alt or key.ctrl:
                self.cursor = self._word_right() if forward else self._word_left()
            else:
                self.cursor = (
                    min(self.cursor + 1, len(self.text)) if forward else max(self.cursor - 1, 0)
                )
            return True
        if name == "home":
            self.cursor = 0 if key.ctrl or key.super else self._line_start()
            return True
        if name == "end":
            self.cursor = len(self.text) if key.ctrl or key.super else self._line_end()
            return True
        if name in ("up", "down"):
            if key.super or key.ctrl:
                self.cursor = 0 if name == "up" else len(self.text)
                return True
            return self._vertical(-1 if name == "up" else 1)
        return False

    def _control(self, letter: str) -> bool:
        if letter == "a":
            self.cursor = self._line_start()
        elif letter == "e":
            self.cursor = self._line_end()
        elif letter == "b":
            self.cursor = max(self.cursor - 1, 0)
        elif letter == "f":
            self.cursor = min(self.cursor + 1, len(self.text))
        elif letter == "u":
            self._delete(0, self.cursor)
        elif letter == "w":
            self._delete(self._word_left(), self.cursor)
        elif letter == "k":
            end = self._line_end()
            self._delete(self.cursor, end if end > self.cursor else min(end + 1, len(self.text)))
        elif letter == "d":
            self._delete(self.cursor, min(self.cursor + 1, len(self.text)))
        elif letter == "h":
            if self.cursor:
                self._delete(self.cursor - 1, self.cursor)
        else:
            return False
        return True

    def _meta(self, letter: str) -> bool:
        if letter == "b":
            self.cursor = self._word_left()
        elif letter == "f":
            self.cursor = self._word_right()
        elif letter == "d":
            self._delete(self.cursor, self._word_right())
        else:
            return False
        return True

    def insert(self, text: str) -> None:
        self.text = self.text[: self.cursor] + text + self.text[self.cursor:]
        self.cursor += len(text)

    def _delete(self, start: int, end: int) -> None:
        if end > start:
            self.text = self.text[:start] + self.text[end:]
            self.cursor = start

    def _line_start(self) -> int:
        return self.text.rfind("\n", 0, self.cursor) + 1

    def _line_end(self) -> int:
        end = self.text.find("\n", self.cursor)
        return len(self.text) if end < 0 else end

    def _word_left(self) -> int:
        index = self.cursor
        while index and not _wordish(self.text[index - 1]):
            index -= 1
        while index and _wordish(self.text[index - 1]):
            index -= 1
        return index

    def _word_right(self) -> int:
        index, size = self.cursor, len(self.text)
        while index < size and not _wordish(self.text[index]):
            index += 1
        while index < size and _wordish(self.text[index]):
            index += 1
        return index

    def _vertical(self, direction: int) -> bool:
        start = self._line_start()
        column = self._column if self._column is not None else self.cursor - start
        if direction < 0:
            if start == 0:
                return False
            previous = self.text.rfind("\n", 0, start - 1) + 1
            self.cursor = min(previous + column, start - 1)
        else:
            end = self._line_end()
            if end >= len(self.text):
                return False
            following_end = self.text.find("\n", end + 1)
            following_end = len(self.text) if following_end < 0 else following_end
            self.cursor = min(end + 1 + column, following_end)
        self._column = column
        return True

    @property
    def on_first_line(self) -> bool:
        return self._line_start() == 0

    @property
    def on_last_line(self) -> bool:
        return self._line_end() >= len(self.text)

    def layout(self, width: int) -> tuple[list[str], tuple[int, int]]:
        """Soft-wrapped lines and the cursor's (row, column) within them."""
        width = max(width, 2)
        rows: list[str] = []
        cursor = (0, 0)
        offset = 0
        for line in self.text.split("\n"):
            pieces = _hard_wrap(line, width - 1)
            consumed = 0
            for index, piece in enumerate(pieces):
                begin = offset + consumed
                finish = begin + len(piece)
                last = index == len(pieces) - 1
                if begin <= self.cursor <= finish and (self.cursor < finish or last):
                    cursor = (len(rows), text_width(self.text[begin:self.cursor]))
                rows.append(piece)
                consumed += len(piece)
            offset += len(line) + 1
        return rows, cursor


def _wordish(char: str) -> bool:
    return char.isalnum() or char == "_"


def _hard_wrap(line: str, width: int) -> list[str]:
    """Character wrap: an editor must keep every character where it was typed."""
    if not line:
        return [""]
    pieces, current, used = [], "", 0
    for char in line:
        cells = text_width(char)
        if used + cells > width and current:
            pieces.append(current)
            current, used = "", 0
        current += char
        used += cells
    pieces.append(current)
    return pieces


__all__ = ["TextBuffer"]
