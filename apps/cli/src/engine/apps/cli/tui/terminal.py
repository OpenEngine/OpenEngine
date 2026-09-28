"""The terminal itself: raw input, the alternate screen, and restoring both.

POSIX terminals are put in raw mode with `termios`. Windows consoles are asked
for virtual-terminal input, which makes them send the same escape sequences a
POSIX terminal does, so one decoder serves both.

On start the terminal is also asked for bracketed paste and the kitty keyboard
protocol's "disambiguate" level. A terminal that does not know either ignores
the request, and the legacy spellings the decoder also accepts still work.

The mouse is deliberately never captured, so selecting and copying text works
exactly as it does anywhere else in the terminal. The wheel still scrolls:
"alternate scroll" (`?1007`) has the terminal send ↑/↓ for the wheel while the
alternate screen is showing, which iTerm2, Terminal.app, kitty, WezTerm,
Ghostty, VTE terminals and Windows Terminal do -- most of them by default.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from typing import TextIO

ENTER = (
    "\x1b[?1049h"  # alternate screen
    "\x1b[?25l"  # hide the cursor until a text field wants it
    "\x1b[?2004h"  # bracketed paste
    "\x1b[>1u"  # kitty keyboard protocol: disambiguate escape codes
    "\x1b[?1007h"  # alternate scroll: the wheel sends ↑/↓, the mouse stays free
)
LEAVE = "\x1b[?1007l\x1b[<u\x1b[?2004l\x1b[?25h\x1b[0m\x1b[?1049l"


class Terminal:
    def __init__(self, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self._restore: object = None

    def __enter__(self) -> Terminal:
        if os.name == "nt":
            self._restore = _windows_enter()
        else:
            import termios
            import tty

            descriptor = self.stdin.fileno()
            self._restore = termios.tcgetattr(descriptor)
            tty.setraw(descriptor)
        self.write(ENTER)
        return self

    def __exit__(self, *_exc: object) -> None:
        self.write(LEAVE)
        if os.name == "nt":
            _windows_leave(self._restore)
        elif self._restore is not None:
            import termios

            termios.tcsetattr(self.stdin.fileno(), termios.TCSADRAIN, self._restore)

    def size(self) -> tuple[int, int]:
        columns, rows = shutil.get_terminal_size((100, 30))
        return max(columns, 20), max(rows, 8)

    def write(self, data: str) -> None:
        self.stdout.write(data)
        self.stdout.flush()

    def read(self, timeout: float) -> str:
        """Whatever has been typed, waiting at most `timeout` for the first of it."""
        if os.name == "nt":
            return _windows_read(timeout)
        import select

        descriptor = self.stdin.fileno()
        ready, _, _ = select.select([descriptor], [], [], timeout)
        if not ready:
            return ""
        # os.read rather than the text wrapper, which would buffer ahead and
        # make select report an escape sequence's tail as not yet arrived.
        data = os.read(descriptor, 4096)
        return data.decode("utf-8", errors="replace")


def _windows_enter() -> object:
    import ctypes

    kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
    stdin, stdout = kernel.GetStdHandle(-10), kernel.GetStdHandle(-11)
    before_in, before_out = ctypes.c_uint(), ctypes.c_uint()
    kernel.GetConsoleMode(stdin, ctypes.byref(before_in))
    kernel.GetConsoleMode(stdout, ctypes.byref(before_out))
    # Virtual-terminal input, and none of line editing, echo or Ctrl+C
    # handling. QuickEdit is left as it was, so the mouse still selects text.
    kernel.SetConsoleMode(stdin, (before_in.value | 0x0200) & ~(0x0001 | 0x0002 | 0x0004))
    kernel.SetConsoleMode(stdout, before_out.value | 0x0004 | 0x0001)
    return (before_in.value, before_out.value)


def _windows_leave(restore: object) -> None:
    import ctypes

    if not isinstance(restore, tuple):
        return
    kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel.SetConsoleMode(kernel.GetStdHandle(-10), restore[0])
    kernel.SetConsoleMode(kernel.GetStdHandle(-11), restore[1])


def _windows_read(timeout: float) -> str:
    import msvcrt

    deadline = time.monotonic() + timeout
    while not msvcrt.kbhit():  # type: ignore[attr-defined]
        if time.monotonic() >= deadline:
            return ""
        time.sleep(0.01)
    chars = []
    while msvcrt.kbhit():  # type: ignore[attr-defined]
        chars.append(msvcrt.getwch())  # type: ignore[attr-defined]
    return "".join(chars)


__all__ = ["Terminal"]
