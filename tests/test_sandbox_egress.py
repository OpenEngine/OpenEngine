"""Host-enforced egress, including YAML -> ACP node -> real guest connections."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil

import pytest
from langgraph_acp import ACPAgentRegistry, ACPConnectContext, ClaudeACPProvider, CodexACPProvider

from engine.adapters.sandbox.process import ProcessSandbox
from engine.adapters.sandbox.smolvm import SmolvmSandbox
from engine.graph_runtime_langgraph import ACPNode, SandboxACPProvider, SandboxConnectContext
from engine.graph_runtime_langgraph.sandbox import PROVIDER_ALLOWED_HOSTS, sandbox_provider
from engine.graph_service.compile import compile_graph
from engine.graph_service.language import GraphError, parse_graph
from engine.ports import SandboxSpec
from engine.ports.sandbox_network import allowed_hosts
from tests.test_sandbox_acp_provider import run_node


@pytest.mark.parametrize("value", ["example.com", None, 2, [None], [False], [""], ["https://example.com"],
    ["example.com:443"], ["*.example.com"], ["example.com/"], ["example.com."], ["a..com"], ["-bad.com"],
    ["127.0.0.1"], ["::1"], ["10.0.0.0/8"], ["--net"], ["evil\n.com"], ["é.com"]])
def test_invalid_hostname_grants_fail_before_launch(value):
    with pytest.raises(ValueError):
        allowed_hosts(value)
    with pytest.raises(ValueError):
        ACPNode(agent="codex", prompt="test", cwd=".", allowed_hosts=value)
    if value is not None:
        with pytest.raises(ValueError):
            SandboxSpec(egress_allowlist=value)


def document(hosts):
    return {"apiVersion": "openengine.cc/v1", "name": "network", "implementation": {
        "probe": {"agent": "codex", "prompt": "Check connections", "allowed_hosts": hosts},
    }}


@pytest.mark.parametrize("hosts", [None, "example.com", {}, ["https://example.com"], ["*.example.com"], [False]])
def test_yaml_reports_bad_hosts_on_the_node(hosts):
    with pytest.raises(GraphError) as caught:
        parse_graph(document(hosts))
    assert any(problem.path == "implementation.probe.allowed_hosts" for problem in caught.value.problems)


def test_grants_are_normalized_and_yaml_facets_keep_them():
    raw = document(["EXAMPLE.com", "example.com", "api.example.org"])
    raw["implementation"]["probe"]["facets"] = [{"facet": "security"}, {"facet": "bugs"}]
    spec = parse_graph(raw)
    assert spec.nodes[0].allowed_hosts == ("example.com", "api.example.org")
    workflow = compile_graph(spec, version_id="network-v1", number=1, workspace_provider=object(),
        registry=ACPAgentRegistry([CodexACPProvider()]))
    for key in spec.nodes[0].keys:
        node = workflow.builder.nodes[key].runnable.afunc
        assert node.allowed_hosts == ("example.com", "api.example.org")


@pytest.mark.parametrize("kind,provider_type", [("codex", CodexACPProvider), ("claude", ClaudeACPProvider)])
def test_selected_provider_defaults_and_extra_hosts_reach_backend(tmp_path, kind, provider_type):
    seen = []
    class ReachedBackend(Exception):
        pass
    class Backend:
        @asynccontextmanager
        async def create(self, spec):
            seen.append(spec)
            raise ReachedBackend
            yield  # pragma: no cover
    provider = sandbox_provider(provider_type(name="renamed"), Backend())
    async def run():
        with pytest.raises(ReachedBackend):
            await provider.connect(context=SandboxConnectContext(workspace=str(tmp_path),
                sandbox_spec=SandboxSpec(egress_allowlist=("example.com",))))
    asyncio.run(run())
    assert seen[0].egress_allowlist == (*PROVIDER_ALLOWED_HOSTS[kind], "example.com")
    assert "github.com" not in seen[0].egress_allowlist


def test_custom_agents_default_to_deny_all_and_process_cannot_ignore_policy(tmp_path):
    async def run():
        with pytest.raises(ValueError, match="egress"):
            await SandboxACPProvider(name="custom", command=("unused",), backend=ProcessSandbox()).connect(
                context=ACPConnectContext(workspace=str(tmp_path)),
            )
    asyncio.run(run())


@pytest.fixture
def real_backend():
    image = os.environ.get("OE_SMOLVM_TEST_IMAGE")
    if not image:
        pytest.skip("set OE_SMOLVM_TEST_IMAGE for credential-free real VM egress tests")
    return SmolvmSandbox(image=image, executable=os.environ.get("OE_SMOLVM_EXECUTABLE", "smolvm"))


PROBE = '''import socket,json,sys
result = {}
for host in json.loads(sys.argv[1]):
    try:
        with socket.create_connection((host,443),timeout=3):
            result[host] = True
    except OSError:
        result[host] = False
print(json.dumps(result),flush=True)
'''


@pytest.mark.integration
def test_real_guest_deny_all(real_backend):
    async def run():
        async with real_backend.create(SandboxSpec(egress_allowlist=(), timeout=30)) as vm:
            p = await vm.exec(("python3", "-c", PROBE, json.dumps(["example.com", "1.1.1.1"])))
            result = json.loads(await p.stdout.read())
            assert await p.wait() == 0
            assert result == {"example.com": False, "1.1.1.1": False}
    asyncio.run(run())


@pytest.mark.integration
@pytest.mark.parametrize("provider_type", [CodexACPProvider, ClaudeACPProvider])
def test_yaml_node_extra_host_and_provider_connections_inside_real_vm(real_backend, tmp_path, provider_type):
    # Real ACP pipes and real host-enforced networking, with a deterministic peer
    # in place of paid model calls. No host credential store is consulted.
    source = tmp_path / "source"
    source.mkdir()
    shutil.copy2(Path(__file__).with_name("sandbox_acp_agent.py"), source / "agent.py")
    (source / "input.txt").write_text("workspace-marker")
    kind = "codex" if provider_type is CodexACPProvider else "claude"
    provider_hosts = PROVIDER_ALLOWED_HOSTS[kind]
    probes = [*provider_hosts, "example.com", "example.org", "www.example.com", "1.1.1.1"]
    peer = source / "agent.py"
    text = peer.read_text().replace('        if "--large" in sys.argv:', '''        import subprocess
        data["network"] = json.loads(subprocess.check_output([sys.executable, "-c", sys.argv[-2], sys.argv[-1]]))
        if "--large" in sys.argv:''')
    peer.write_text(text)
    registry = ACPAgentRegistry([provider_type(name="codex", command=("python3", "agent.py", PROBE, json.dumps(probes)))])
    spec = parse_graph(document(["example.com"]))
    workflow = compile_graph(spec, version_id="real-network-v1", number=1, workspace_provider=object(), registry=registry)
    compiled_node = workflow.builder.nodes["probe"].runnable.afunc
    node = replace(compiled_node, cwd=str(source), output_key="agent", state_key="agent")
    result = asyncio.run(run_node(node, backend=real_backend, timeout=90))
    network = json.loads(result)["network"]
    assert all(network[host] for host in provider_hosts), network
    assert network["example.com"], network
    assert not any(network[host] for host in ("example.org", "www.example.com", "1.1.1.1")), network


def test_python_node_grants_do_not_leak_to_another_node(smolvm_cli, tmp_path):
    launcher, root = smolvm_cli
    source = tmp_path / "source"
    source.mkdir()
    shutil.copy2(Path(__file__).with_name("sandbox_acp_agent.py"), source / "agent.py")
    (source / "input.txt").write_text("workspace-marker")
    import sys
    provider = CodexACPProvider(command=(sys.executable, "agent.py"))
    registry = ACPAgentRegistry([provider])
    backend = SmolvmSandbox("fixture", executable=str(launcher))
    async def run():
        for extra in ("example.com", "example.org"):
            await run_node(ACPNode(agent=provider.name, registry=registry, cwd=str(source), prompt="hello",
                allowed_hosts=(extra,)), backend=backend)
    asyncio.run(run())
    commands = [json.loads(line) for line in (root / "commands.jsonl").read_text().splitlines()]
    creates = [args for args in commands if args[:2] == ["machine", "create"]]
    grants = [tuple(args[index + 1] for index, flag in enumerate(args) if flag == "--allow-host-pattern")
              for args in creates]
    assert grants == [(*PROVIDER_ALLOWED_HOSTS["codex"], "example.com"), (*PROVIDER_ALLOWED_HOSTS["codex"], "example.org")]
    assert all("--net" not in args and "--allow-host" not in args for args in creates)


def test_host_node_cannot_silently_ignore_explicit_grants(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from engine.graph_runtime_langgraph import acp
    monkeypatch.setattr(acp, "current_execution", lambda: SimpleNamespace(
        runtime=SimpleNamespace(sandbox=None), run_id="run", node_id="node", execution_id="exec",
    ))
    node = ACPNode(agent="codex", cwd=str(tmp_path), prompt="test", allowed_hosts=("example.com",))
    async def run():
        with pytest.raises(ValueError, match="egress enforcement"):
            await node._open(None, str(tmp_path), ())
    asyncio.run(run())
