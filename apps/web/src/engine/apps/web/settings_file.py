"""Writing the small JSON files the web app keeps its settings and caches in."""

import json
import os
from pathlib import Path
from uuid import uuid4


def atomic_write_json(path: Path, value: object) -> None:
    """Replace `path` whole, so a stop mid-write leaves the previous contents.

    The scratch file is named per write: two writers sharing one scratch name
    could each rename the file the other is still filling, leaving a half
    written file in place. With a name each, the last to finish wins whole.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        # Only reached with a file left when the rename did not happen; a
        # scratch file left behind would never be read or cleaned up.
        temporary.unlink(missing_ok=True)
