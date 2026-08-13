"""Reading JSONL from a subprocess pipe, without a line-length ceiling.

`asyncio.StreamReader.readline` buffers up to 64 KiB and raises
`ValueError: Separator is found, but chunk is longer than limit` on anything
longer. Every CLI-shaped agent runner hits this for the same reason: one tool
result is one JSONL event on one line, and reading a source file of a few
thousand lines produces an event well past 64 KiB. The failure is not a partial
read -- the turn dies mid-stream and the work is lost.

So this lives here rather than in any one adapter: it is identical work for all
of them, and the version that only fixed the adapter someone happened to be
running left the other one waiting to hit the same ceiling.
"""

import asyncio
from collections.abc import AsyncIterator

#: How much to pull from the pipe per read. Not a limit on the line -- a line
#: longer than this simply spans several chunks.
CHUNK_SIZE = 64 * 1024


async def read_lines(stream: asyncio.StreamReader) -> AsyncIterator[bytes]:
    """Yield newline-terminated chunks of `stream`, of any length.

    The trailing fragment is yielded too if the stream ends without a newline,
    since a final event is still an event.
    """
    pending = bytearray()
    search_from = 0
    while chunk := await stream.read(CHUNK_SIZE):
        pending.extend(chunk)
        while (newline := pending.find(b"\n", search_from)) >= 0:
            yield bytes(pending[:newline])
            del pending[: newline + 1]
            search_from = 0
        search_from = len(pending)
    if pending:
        yield bytes(pending)
