"""Test-only daemon with a childless ACP fixture; never a production fencing option."""
import asyncio
from pathlib import Path
import sys
from virtual_control_plane.daemon.codex import CodexBackend
from virtual_control_plane.daemon.service import parser, serve
from virtual_control_plane.tests.test_codex_executor import FixtureFence

if __name__ == '__main__':
    arguments = parser()
    arguments.add_argument('--acp-log', required=True)
    arguments.add_argument('--acp-mode', default='normal')
    args = arguments.parse_args()
    fence = FixtureFence()
    backend = CodexBackend([sys.executable, str(Path(__file__).with_name('acp_agent.py')), args.acp_log, args.acp_mode], fence)
    backend.spawn = fence.spawn
    asyncio.run(serve(args, backends={'codex': backend}))
