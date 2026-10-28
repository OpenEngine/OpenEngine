"""The tunnel guide's token step never leaves the token readable by other users.

Setup: a fresh HOME with `~/.config/openengine/` and no `.env` yet (nothing
else creates it), under the common umask 022, as on a new Mac mini.

Command: each line of the published guide's token block, run in order in that
shell (skipping `oe daemon stop`), checking the file's mode after each line.

Expected: once the token is written, `.env` is never group- or world-readable.
Actual: the `printf ... >> ~/.config/openengine/.env` line creates it 0644, so
other local users can read the token until (and unless) `chmod 600` runs.
"""

import re
import stat
import subprocess
from pathlib import Path

GUIDE = (
    Path(__file__).resolve().parents[1]
    / "site/docs/guides/test2doc-other-computers/test2doc-reach-your-openengine-from-anywhere.mdx"
)


def _token_blocks() -> list[list[str]]:
    blocks = re.findall(r"```sh\n(.*?)```", GUIDE.read_text(), flags=re.S)
    return [
        [line for line in block.splitlines() if line.strip() and not line.startswith("oe ")]
        for block in blocks
        if "ENGINE_SERVICE_TOKEN" in block
    ]


def test_the_guides_token_file_is_never_readable_by_others(tmp_path: Path) -> None:
    blocks = _token_blocks()
    assert blocks, "the guide has no token step"
    for number, lines in enumerate(blocks):
        home = tmp_path / f"home-{number}"
        (home / ".config/openengine").mkdir(parents=True)
        env_file = home / ".config/openengine/.env"
        for line in lines:
            subprocess.run(
                ["/bin/sh", "-c", f"umask 022\n{line}"],
                env={"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"},
                check=True,
            )
            if env_file.exists():
                mode = stat.S_IMODE(env_file.stat().st_mode)
                assert mode & 0o077 == 0, (
                    f"after `{line}`, the token file is mode {oct(mode)}; other users can read it"
                )
