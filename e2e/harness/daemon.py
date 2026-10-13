"""engine-web, with every agent replaced by the scripted ACP fake.

The CLI e2e tests' stub mode: the production daemon, composed by its own
`main()` from the same configuration and environment, except that each ACP
provider -- Claude, Codex and OpenCode, wherever the daemon builds one --
launches `tests/provider_fakes.py`'s agent instead of the real adapter. What
that agent says and does is read from `ENGINE_FAKE_SCRIPT` on every turn; see
the module docstring there.

Started by `e2e/fixtures/engine.ts` with the repository's interpreter. Real
mode runs `engine-web` itself and needs none of this.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

#: e2e/harness/daemon.py -> the repository root.
REPO_ROOT = Path(__file__).resolve().parents[2]

# The fake is shared with the pytest tier and the web e2e harness, where it lives.
sys.path.insert(0, str(REPO_ROOT / "tests"))

from langgraph_acp.providers import claude, codex, opencode  # noqa: E402
from provider_fakes import fake_acp  # noqa: E402


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: daemon.py BIN_DIRECTORY", file=sys.stderr)
        return 2
    binaries = Path(sys.argv[1])
    binaries.mkdir(parents=True, exist_ok=True)
    fake = (fake_acp(binaries),)

    # Every provider resolves its command through `launch_command` in
    # `__post_init__`; answering with the fake there swaps the agent and
    # leaves the rest of the composition exactly as production builds it.
    def launch_fake(_command: Sequence[str]) -> tuple[str, ...]:
        return fake

    for module in (claude, codex, opencode):
        module.launch_command = launch_fake  # type: ignore[attr-defined]

    from engine.apps.web.__main__ import main as engine_web

    return engine_web([])


if __name__ == "__main__":
    raise SystemExit(main())
