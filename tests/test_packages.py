"""The scaffold exists and can be imported.

Ticket 1's first acceptance criterion, plus a check that each of the six named
capabilities actually has a port and at least one adapter aimed at it.
"""

import importlib

import pytest

from layout import PACKAGES, Package

#: Straight from the ticket's tree. Hard-coded on purpose -- discovery finding
#: five packages and calling it a day would pass a discovery-driven test.
EXPECTED_PACKAGE_ROOTS = [
    "packages/domain",
    "packages/engine",
    "packages/ports",
    "packages/runtime",
    "packages/web",
    "packages/adapters/temporal",
    "packages/adapters/github",
    "packages/adapters/openai",
    "packages/adapters/anthropic",
    "packages/adapters/scripted",
    "packages/adapters/communications",
    "packages/adapters/workspace",
    "packages/adapters/postgres",
    "apps/control_server",
    "apps/worker",
]

#: Capability -> the port protocol that defines it.
CAPABILITIES = {
    "Workflow Runtime": "WorkflowRuntime",
    "Source Control": "SourceControl",
    "Agent Runner": "AgentRunner",
    "Communications": "Communications",
    "Workspace Provider": "WorkspaceProvider",
    "State Store": "StateStore",
}

#: Capability -> an implementation of it.
IMPLEMENTATIONS = {
    "Workflow Runtime": ("engine.adapters.temporal", "TemporalWorkflowRuntime"),
    "Source Control": ("engine.adapters.github", "GitHubSourceControl"),
    "Agent Runner": ("engine.adapters.openai", "OpenAIAgentRunner"),
    "Communications": ("engine.adapters.communications", "BuzzCommunications"),
    "Workspace Provider": ("engine.adapters.workspace", "GitWorktreeWorkspaceProvider"),
    "State Store": ("engine.adapters.postgres", "PostgresStateStore"),
}

#: Agent runners registered as plugins. Two real ones plus a placeholder, so the
#: port is proven against more than a single vendor.
AGENT_RUNNERS = ["anthropic", "openai", "scripted"]

ALL_MODULES = sorted(m for p in PACKAGES for m in p.modules)


def test_every_expected_package_exists() -> None:
    found = {p.rel_root for p in PACKAGES}
    missing = [root for root in EXPECTED_PACKAGE_ROOTS if root not in found]
    assert not missing, f"scaffold is incomplete: {missing}"


@pytest.mark.parametrize("module", ALL_MODULES)
def test_module_is_importable(module: str) -> None:
    importlib.import_module(module)


@pytest.mark.parametrize("package", PACKAGES, ids=[p.rel_root for p in PACKAGES])
def test_package_ships_at_least_one_module(package: Package) -> None:
    assert package.modules, f"{package.dist_name} has no importable module under src/"


@pytest.mark.parametrize("capability,protocol", sorted(CAPABILITIES.items()))
def test_capability_has_a_port(capability: str, protocol: str) -> None:
    ports = importlib.import_module("engine.ports")
    assert hasattr(ports, protocol), f"{capability} has no port named {protocol}"


@pytest.mark.parametrize("capability,target", sorted(IMPLEMENTATIONS.items()))
def test_capability_has_a_placeholder_implementation(
    capability: str, target: tuple[str, str]
) -> None:
    module_name, class_name = target
    module = importlib.import_module(module_name)
    assert hasattr(module, class_name), f"{capability}: {module_name}.{class_name} is missing"


@pytest.mark.parametrize("name", AGENT_RUNNERS)
def test_agent_runner_is_registered_as_a_plugin(name: str) -> None:
    """Backends must be discoverable by name, not only by import.

    This is what lets a consumer install their own adapter and select it with
    `ENGINE_AGENT_RUNNER` instead of forking the control server.
    """
    from engine.runtime import available_agent_runners

    assert name in available_agent_runners()


def test_registered_runners_satisfy_the_port() -> None:
    """Every plugin builds something with the shape the ports declare."""
    from engine.ports import AgentRunner
    from engine.runtime import RunnerUnavailable, load_agent_runner

    for name in AGENT_RUNNERS:
        try:
            runner = load_agent_runner(name)
        except RunnerUnavailable:
            continue  # installed but unusable here (e.g. no credentials)
        assert isinstance(runner, AgentRunner), f"{name} does not satisfy AgentRunner"


def test_capabilities_container_covers_every_port() -> None:
    """`Capabilities` is the runtime's one slot per capability -- keep it total."""
    from engine.runtime import Capabilities

    expected = {
        "workflow_runtime",
        "source_control",
        "agent_runner",
        "communications",
        "workspace_provider",
        "state_store",
    }
    assert set(Capabilities.__dataclass_fields__) == expected
