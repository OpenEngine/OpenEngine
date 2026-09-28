"""Turn what a terminal sends into key presses.

Terminals disagree about how to spell anything past a letter, so this accepts
every common spelling of the keys the workbench uses rather than one:

* legacy control bytes (`^A`, `^E`, `^U`, `^W`, DEL), which is what macOS
  terminals send for Cmd+Left/Right/Backspace under "natural text editing";
* `ESC` prefixes for Option/Alt (`ESC b`, `ESC f`, `ESC DEL`, `ESC CR`);
* xterm modifier parameters (`CSI 1;3D` is Alt+Left, `CSI 1;9D` Cmd+Left);
* xterm `modifyOtherKeys` (`CSI 27;2;13~` is Shift+Enter);
* the kitty keyboard protocol (`CSI 13;2u`), which the terminal is asked for
  on start and which is how Shift+Enter becomes distinguishable at all;
* bracketed paste, so a pasted newline is text rather than a send;
* SGR mouse reports (`CSI < b;x;y M`), for the wheel and dragging.

`Enter` sends. A newline in the text is any of Shift+Enter, Alt/Option+Enter,
Ctrl+Enter, or Ctrl+J -- the last being what Windows consoles report for
Ctrl+Enter and what many terminals are configured to send for Shift+Enter.
"""

from __future__ import annotations

from dataclasses import dataclass

ESC = "\x1b"
PASTE_START = "\x1b[200~"
PASTE_END = "\x1b[201~"


@dataclass(frozen=True, slots=True)
class Key:
    """One key press.

    `name` is `"char"` for printable text (in `text`), `"paste"` for a
    bracketed paste, and otherwise a key name: `enter`, `newline`, `tab`,
    `backtab`, `backspace`, `delete`, `escape`, `up`, `down`, `left`, `right`,
    `home`, `end`, `pageup`, `pagedown`. Control letters arrive as their
    letter with `ctrl` set. `super` is Cmd on macOS and the Windows key.

    The mouse arrives as `wheelup`, `wheeldown`, and the left button's
    `press`, `drag` and `release`, at 0-based screen cell `x`, `y`.
    """

    name: str
    text: str = ""
    ctrl: bool = False
    alt: bool = False
    shift: bool = False
    super: bool = False
    x: int = 0
    y: int = 0

    @property
    def mouse(self) -> bool:
        return self.name in ("wheelup", "wheeldown", "press", "drag", "release")

    @property
    def plain(self) -> bool:
        return not (self.ctrl or self.alt or self.super)

    def is_char(self, text: str) -> bool:
        return self.name == "char" and self.text == text and self.plain


_CSI_LETTERS = {
    "A": "up", "B": "down", "C": "right", "D": "left",
    "H": "home", "F": "end", "Z": "backtab",
}
_CSI_TILDES = {
    1: "home", 7: "home", 4: "end", 8: "end", 3: "delete",
    5: "pageup", 6: "pagedown", 2: "insert",
}
#: Kitty's functional keys that have a legacy code point of their own.
_CODEPOINTS = {13: "enter", 27: "escape", 9: "tab", 127: "backspace", 8: "backspace"}


def _modifiers(value: int) -> dict[str, bool]:
    """xterm/kitty modifier parameter: 1 + a bitmask."""
    bits = max(value - 1, 0)
    return {
        "shift": bool(bits & 1),
        # Meta (32) is Alt on every keyboard this is likely to meet.
        "alt": bool(bits & 2) or bool(bits & 32),
        "ctrl": bool(bits & 4),
        "super": bool(bits & 8),
    }


def _codepoint_key(code: int, mods: dict[str, bool]) -> Key:
    name = _CODEPOINTS.get(code)
    if name == "enter" and (mods["shift"] or mods["alt"] or mods["ctrl"]):
        return Key("newline")
    if name is not None:
        if name == "tab" and mods["shift"]:
            return Key("backtab")
        return Key(name, **mods)
    # Kitty's private-use code points are modifier and keypad keys on their own.
    if not 32 <= code < 0x110000 or 0xE000 <= code <= 0xF8FF:
        return Key("unknown")
    char = chr(code)
    if mods["shift"] and char.isalpha():
        char = char.upper()
    return Key("char", char, ctrl=mods["ctrl"], alt=mods["alt"], super=mods["super"])


class KeyDecoder:
    """Incremental: feed it text as it arrives, take keys out.

    A lone `ESC` is ambiguous until the bytes after it have had a moment to
    arrive, so `feed` holds an unfinished sequence back and `flush` -- called
    when the input has gone quiet -- decides it was the Escape key after all.
    """

    def __init__(self) -> None:
        self._pending = ""
        self._paste: list[str] | None = None

    @property
    def waiting(self) -> bool:
        return bool(self._pending) and self._paste is None

    def feed(self, data: str) -> list[Key]:
        self._pending += data
        keys: list[Key] = []
        while self._pending:
            if self._paste is not None:
                end = self._pending.find(PASTE_END)
                if end < 0:
                    # Keep a possible partial terminator for the next read.
                    keep = _partial_suffix(self._pending, PASTE_END)
                    self._paste.append(self._pending[: len(self._pending) - keep])
                    self._pending = self._pending[len(self._pending) - keep:]
                    break
                self._paste.append(self._pending[:end])
                self._pending = self._pending[end + len(PASTE_END):]
                keys.append(Key("paste", "".join(self._paste)))
                self._paste = None
                continue
            key, used = self._decode(self._pending)
            if used == 0:
                break
            self._pending = self._pending[used:]
            if key is not None:
                keys.append(key)
        return keys

    def flush(self) -> list[Key]:
        """The input went quiet: whatever is held back is complete."""
        if self._paste is not None or not self._pending:
            return []
        pending, self._pending = self._pending, ""
        keys: list[Key] = []
        if pending.startswith(ESC):
            rest = pending[1:]
            if not rest:
                return [Key("escape")]
            if rest.startswith(("[", "O")):
                # An unfinished sequence that will never finish.
                return [Key("escape")]
            keys.append(Key("escape"))
            pending = rest
        keys.extend(self.feed(pending))
        return keys

    def _decode(self, data: str) -> tuple[Key | None, int]:
        """The first key in `data` and how many characters it used; 0 = need more."""
        char = data[0]
        if char != ESC:
            return _single(char), 1
        if len(data) == 1:
            return None, 0
        second = data[1]
        if second == "[":
            return self._csi(data)
        if second == "O":
            if len(data) < 3:
                return None, 0
            name = _CSI_LETTERS.get(data[2])
            return (Key(name) if name else None), 3
        if second == ESC:
            # ESC ESC: some terminals' Alt+Escape, or Escape pressed twice.
            return Key("escape"), 1
        inner = _single(second)
        if inner is None:
            return None, 2
        if inner.name == "enter":
            return Key("newline"), 2
        return Key(inner.name, inner.text, ctrl=inner.ctrl, alt=True, shift=inner.shift), 2

    def _csi(self, data: str) -> tuple[Key | None, int]:
        index = 2
        while index < len(data) and not ("\x40" <= data[index] <= "\x7e"):
            index += 1
        if index >= len(data):
            # Guard against a runaway sequence swallowing everything typed next.
            if len(data) > 32:
                return Key("escape"), 1
            return None, 0
        params, final = data[2:index], data[index]
        used = index + 1
        if data[:used] == PASTE_START:
            self._paste = []
            return None, used
        if params.startswith("<") and final in "Mm":
            return _mouse(params[1:], final == "M"), used
        if params.startswith(("<", ">", "?", "=")):
            return None, used  # A report, not a key.
        fields = [part.split(":")[0] for part in params.split(";")]
        numbers = [int(part) if part.isdigit() else 0 for part in fields]
        if final == "u":
            code = numbers[0] if numbers else 0
            mods = _modifiers(numbers[1] if len(numbers) > 1 else 1)
            return _codepoint_key(code, mods), used
        if final == "~":
            first = numbers[0] if numbers else 0
            if first == 27 and len(numbers) >= 3:
                return _codepoint_key(numbers[2], _modifiers(numbers[1])), used
            name = _CSI_TILDES.get(first)
            if name is None:
                return None, used
            mods = _modifiers(numbers[1] if len(numbers) > 1 else 1)
            return Key(name, **mods), used
        name = _CSI_LETTERS.get(final)
        if name is None:
            return None, used  # Focus events and friends.
        if name == "backtab":
            return Key("backtab"), used
        mods = _modifiers(numbers[1] if len(numbers) > 1 else 1)
        return Key(name, **mods), used


def _mouse(params: str, pressed: bool) -> Key | None:
    """One SGR mouse report: button;column;row, 1-based."""
    try:
        button, column, row = (int(part) for part in params.split(";"))
    except ValueError:
        return None
    where = {"x": column - 1, "y": row - 1}
    if button & 64:
        direction = button & 3
        return (
            Key("wheelup", **where) if direction == 0
            else Key("wheeldown", **where) if direction == 1
            else None  # Sideways scrolling.
        )
    if button & 3 != 0:
        return None  # Only the left button selects.
    if not pressed:
        return Key("release", **where)
    return Key("drag" if button & 32 else "press", **where)


def _single(char: str) -> Key | None:
    if char == "\r":
        return Key("enter")
    if char == "\n":
        return Key("newline")
    if char == "\t":
        return Key("tab")
    if char == "\x7f":
        return Key("backspace")
    if char == "\x08":
        # Ctrl+Backspace on Windows and in most Linux terminals.
        return Key("backspace", ctrl=True)
    if char == "\x00":
        return Key("char", " ", ctrl=True)
    if "\x01" <= char <= "\x1a":
        return Key("char", chr(ord(char) + 96), ctrl=True)
    if char < " ":
        return None
    return Key("char", char)


def _partial_suffix(data: str, marker: str) -> int:
    for size in range(min(len(marker) - 1, len(data)), 0, -1):
        if marker.startswith(data[-size:]):
            return size
    return 0


__all__ = ["Key", "KeyDecoder"]
