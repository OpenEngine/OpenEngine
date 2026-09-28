"""`engine` with no arguments: the terminal workbench.

    work orders | graph            (home)
    graph       | in progress      (enter on a WorkOrder)
    orders | graph | conversation  (enter on a node)

Enter goes in, Esc comes back out, j/k (or the arrows) move. The mouse is left
to the terminal, so text selects and copies as usual; the wheel arrives as the
arrow keys. See `app` for the
screens and `editor` for the text-editing keys.
"""

from __future__ import annotations

import threading

from engine.apps.cli.tui.app import App
from engine.apps.cli.tui.client import ServiceClient
from engine.apps.cli.tui.keys import KeyDecoder
from engine.apps.cli.tui.terminal import Terminal

POLL_SECONDS = 1.0


def run_workbench(app: App, terminal: Terminal | None = None) -> int:
    terminal = terminal or Terminal()
    stop = threading.Event()

    def poller() -> None:
        while not stop.is_set():
            try:
                app.poll()
            except Exception as error:  # noqa: BLE001 -- keep the screen alive
                app.post(lambda error=error: app._fail(f"refresh failed: {error}"))
            stop.wait(POLL_SECONDS)

    threading.Thread(target=poller, daemon=True).start()
    decoder = KeyDecoder()
    previous: list[str] = []
    size = (0, 0)
    with terminal:
        try:
            while not app.quit:
                app.drain()
                current = terminal.size()
                if current != size:
                    size, previous = current, []
                    terminal.write("\x1b[2J")
                lines, cursor = app.render(*size)
                output = [
                    f"\x1b[{row + 1};1H{line}"
                    for row, line in enumerate(lines)
                    if row >= len(previous) or previous[row] != line
                ]
                if cursor is not None:
                    output.append(f"\x1b[{cursor[0] + 1};{cursor[1] + 1}H\x1b[?25h")
                else:
                    output.append("\x1b[?25l")
                terminal.write("".join(output))
                previous = lines
                data = terminal.read(0.03 if decoder.waiting else 0.2)
                keys = decoder.feed(data) if data else decoder.flush()
                for key in keys:
                    app.handle(key)
                    if app.quit:
                        break
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
    return 0


__all__ = ["App", "ServiceClient", "run_workbench"]
