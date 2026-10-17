"""Opt-in real Codex and Claude nodes in SmolVM; never substitute a CLI fake.

OE_SMOLVM_TEST_IMAGE must name the built guest image. Selected host file/token
logins or explicit model API keys are supported. Once an image is selected, missing credentials,
unsupported hardware, boot errors and provider failures are test failures.
"""

import asyncio
from contextlib import asynccontextmanager
import json
import os

import pytest

from engine.adapters.sandbox.smolvm import SmolvmSandbox
from engine.adapters.sandbox.smolvm._guest import GUEST_ENV_ALLOWLIST
from engine.graph_runtime_langgraph import ACPNode, SandboxACPProvider
from engine.ports import SandboxSpec
from engine.runtime.model_credentials import ModelCredentialError, host_model_credential
from langgraph_acp import ACPAgentRegistry, ACPPermissionOutcome
from test_sandbox_acp_provider import run_node

pytestmark = [pytest.mark.compatibility, pytest.mark.integration]


@pytest.mark.parametrize("agent,command,credential", [
    ("codex", "codex-acp", "OPENAI_API_KEY"),
    ("claude", "claude-agent-acp", "ANTHROPIC_API_KEY"),
])
def test_real_agent_node_in_smolvm(agent, command, credential, tmp_path, monkeypatch):
    image = os.environ.get("OE_SMOLVM_TEST_IMAGE")
    if not image:
        pytest.skip("set OE_SMOLVM_TEST_IMAGE to opt into real SmolVM agent tests")
    try:
        selected = host_model_credential(agent, {credential: os.environ[credential]} if os.environ.get(credential) else {})
    except ModelCredentialError as error:
        pytest.fail(str(error))
    backend = SmolvmSandbox(image=image, executable=os.environ.get("OE_SMOLVM_EXECUTABLE", "smolvm"))
    source = tmp_path / "workspace"
    source.mkdir()
    marker = "smolvm-workspace-marker-674"
    (source / "input.txt").write_text(marker)
    # Record names only: credentials must never be exported into test artifacts.
    (source / "launch.py").write_text(
        "import json, os, pathlib, sys\n"
        "pathlib.Path('launch-environment.json').write_text(json.dumps(sorted(os.environ)))\n"
        "os.execvp(sys.argv[1], sys.argv[1:])\n"
    )
    report = tmp_path / "environment.json"

    class CaptureLaunch:
        @asynccontextmanager
        async def create(self, spec):
            async with backend.create(spec) as sandbox:
                try:
                    yield sandbox
                finally:
                    await sandbox.copy_out("launch-environment.json", report)

    async def allow_test_read(request):
        option = next((item for item in request.options if item.kind == "allow_once"), None)
        return ACPPermissionOutcome.selected(option.option_id) if option else ACPPermissionOutcome.cancelled()

    monkeypatch.setenv("OE_UNSELECTED_HOST_SECRET", "must-not-reach-guest")
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-reach-guest")
    provider = SandboxACPProvider(
        name=agent, command=("python3", "launch.py", command), backend=CaptureLaunch(),
        permissions=allow_test_read,
        model_provider=agent, credential_source=lambda _provider, _overrides: selected,
    )
    node = ACPNode(
        agent=agent, registry=ACPAgentRegistry([provider]), cwd=str(source),
        prompt="Read input.txt from the current workspace and reply with its exact contents. Do not edit files.",
        sandbox_spec=SandboxSpec(timeout=180),
    )
    result = asyncio.run(asyncio.wait_for(run_node(node, timeout=240), 300))
    assert marker in result
    keys = set(json.loads(report.read_text()))
    expected = {"GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL"}
    if agent == "codex":
        expected.add("CODEX_HOME")
        assert "OPENAI_API_KEY" not in keys, "Codex credentials belong in its tmpfs auth.json"
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in keys
    else:
        expected.update({"CLAUDE_CONFIG_DIR", "ANTHROPIC_API_KEY" if selected.kind == "api-key" else "CLAUDE_CODE_OAUTH_TOKEN"})
    assert keys <= GUEST_ENV_ALLOWLIST | expected
    assert "OE_UNSELECTED_HOST_SECRET" not in keys
    assert "GITHUB_TOKEN" not in keys
