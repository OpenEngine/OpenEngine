"""Deployable apps read the process environment only in their `settings.py`.

Every other module takes the values it needs as parameters. `LEGACY` is the
remaining reads still to move behind a `Settings.from_env()` (#741); the list
may only shrink.

The scan is deliberately limited to `apps/`. `packages/` and `workflows/` also
read the environment (git_core, the ACP and sandbox adapters, the GitHub
transports); bringing them under the same rule is tracked as follow-up work on
#741, not enforced here.
"""

import ast
from pathlib import Path

import pytest

ENV_NAMES = {"environ", "getenv"}
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
    aliases = {"os"}
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases |= {a.asname or a.name for a in node.names if a.name == "os"}
        elif isinstance(node, ast.ImportFrom) and node.module in {"os", "posix"}:
            names |= {a.asname or a.name for a in node.names if a.name in ENV_NAMES}
    return bool(names) or any(
        (isinstance(node, ast.Attribute) and node.attr in ENV_NAMES
         and isinstance(node.value, ast.Name) and node.value.id in aliases)
        for node in ast.walk(tree)
    )


def _offenders() -> set[str]:
    return {
        str(path.relative_to(APPS))
        for path in APPS.glob("**/*.py")
        if path.name != "settings.py"
        and "node_modules" not in path.parts
        and ".venv" not in path.parts
        and "tests" not in path.relative_to(APPS).parts
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


def test_mcp_missing_required_variable_wins_over_oidc_conflict():
    from engine.apps.mcp_server.settings import Settings

    with pytest.raises(ValueError, match="OE_MCP_REPOSITORY"):
        Settings.from_env({
            "OE_MCP_WORKFLOW": "w",
            "OE_MCP_PUBLIC_URL": "https://x.example",
            "OE_MCP_OIDC_AUDIENCE": "a",
        })


def test_guard_sees_aliased_and_from_imports(tmp_path):
    for source in ("from os import environ", "import os as o\no.getenv('X')", "from os import getenv as g\ng('X')"):
        module = tmp_path / "m.py"
        module.write_text(source)
        assert _reads_environment(module), source


def test_empty_github_token_falls_back_to_the_config_file():
    from engine.runtime import LoadedEngineConfig, ServiceSettings
    from engine.runtime.config import EngineConfig

    loaded = LoadedEngineConfig(config=EngineConfig(github_token="from-toml"), path=None)
    assert ServiceSettings.from_env(loaded, {"GITHUB_TOKEN": ""}).github_token == "from-toml"
    assert ServiceSettings.from_env(loaded, {"GITHUB_TOKEN": "from-env"}).github_token == "from-env"
