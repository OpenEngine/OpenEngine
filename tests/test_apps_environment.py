"""Deployable apps read the process environment only in their `settings.py`.

Every other module takes the values it needs as parameters. `LEGACY` is the
remaining reads still to move behind a `Settings.from_env()` (#741); the list
may only shrink.
"""

import ast
from pathlib import Path

import pytest

APPS = Path(__file__).resolve().parents[1] / "apps"

LEGACY = {
    "cli/src/engine/apps/cli/daemon.py",
    "cli/src/engine/apps/cli/session.py",
    "open-verify/src/open_verify/agent.py",
    "open-verify/src/open_verify/github.py",
    "open-verify/src/open_verify/playwright_runner.py",
    "web/src/engine/apps/web/__main__.py",
    "web/src/engine/apps/web/dev.py",
    "web/src/engine/apps/web/github_login.py",
    "web/src/engine/apps/web/github_webhook.py",
    "web/src/engine/apps/web/onboarding.py",
    "web/src/engine/apps/web/repositories.py",
    "web/src/engine/apps/web/utilization.py",
}


def _reads_environment(path: Path) -> bool:
    tree = ast.parse(path.read_text())
    return any(
        isinstance(node, ast.Attribute)
        and node.attr in {"environ", "getenv"}
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
        for node in ast.walk(tree)
    )


def _offenders() -> set[str]:
    return {
        str(path.relative_to(APPS))
        for path in APPS.glob("*/src/**/*.py")
        if path.name != "settings.py"
        and "node_modules" not in path.parts
        and ".venv" not in path.parts
        and _reads_environment(path)
    }


def test_no_new_environment_reads_outside_settings():
    assert _offenders() - LEGACY == set()


def test_legacy_list_has_no_stale_entries():
    assert LEGACY - _offenders() == set()


def test_mcp_settings_name_the_missing_variable():
    from engine.apps.mcp_server.settings import Settings

    with pytest.raises(ValueError, match="OE_MCP_WORKFLOW"):
        Settings.from_env({"OE_MCP_REPOSITORY": "o/r", "OE_MCP_PUBLIC_URL": "https://x.example"})
