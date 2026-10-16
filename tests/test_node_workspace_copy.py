"""Private node snapshots, host-owned patches and discard-only review copies.

Host-only patch coordination runs once on the process backend. Transfer,
read-only and graph integration checks still exercise all sandbox backends.
"""

import asyncio
import base64
from contextlib import asynccontextmanager
import os
import stat
from typing import TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph_acp import ACPAgentRegistry, StdioACPProvider

from engine.adapters.sandbox.process import ProcessSandbox
from engine.adapters.sandbox.smolvm import SmolvmSandbox
from engine.graph_runtime import EventLog, GraphId
from engine.graph_runtime_langgraph import ACPNode, LangGraphDefinition, LangGraphRuntime
from engine.graph_runtime_langgraph.components import ReviewNode
from engine.graph_runtime_langgraph.sandbox_workspace import WorkspaceCopies, WorkspacePatchConflict
from engine.ports import SandboxSpec


@pytest.fixture(params=["process", "smolvm-fake", pytest.param("smolvm-real", marks=pytest.mark.integration)])
def backend(request):
    if request.param == "process":
        return ProcessSandbox()
    if request.param == "smolvm-real":
        image = os.environ.get("OE_SMOLVM_TEST_IMAGE")
        if not image:
            pytest.skip("set OE_SMOLVM_TEST_IMAGE for real VM workspace tests; no model credentials needed")
        return SmolvmSandbox(image=image, executable=os.environ.get("OE_SMOLVM_EXECUTABLE", "smolvm"))
    launcher, _ = request.getfixturevalue("smolvm_cli")
    return SmolvmSandbox(image="fixture", executable=str(launcher))


async def command(sandbox, code):
    process = await sandbox.exec(("python3", "-c", code))
    process.stdin.close()
    stdout, stderr, result = await asyncio.gather(process.stdout.read(), process.stderr.read(), process.wait())
    assert result == 0, stderr.decode()
    return stdout.decode()


@pytest.mark.parametrize("backend", ["process"], indirect=True)
def test_changes_are_private_until_accepted_and_later_nodes_see_outputs(backend, git_repo):
    source = git_repo(commit=True)
    (source / "earlier.txt").write_text("earlier node output")

    async def run():
        copies = WorkspaceCopies()
        async with copies.create(str(source), "apply") as first, copies.create(str(source), "apply") as second:
            async with first.backend(backend).create(SandboxSpec()) as vm1:
                await command(vm1, "from pathlib import Path; assert Path('earlier.txt').read_text() == 'earlier node output'; Path('README.md').write_text('first node\\n')")
                async with second.backend(backend).create(SandboxSpec()) as vm2:
                    assert (source / "README.md").read_text() == "engine\n"
                    assert await command(vm2, "from pathlib import Path; print(Path('README.md').read_text(), end='')") == "engine\n"
                # Copy-out is staging only. It does not mutate the host.
                assert (source / "README.md").read_text() == "engine\n"
            assert (source / "README.md").read_text() == "engine\n"
            assert b"first node" in await first.accept()
            assert (source / "README.md").read_text() == "first node\n"
            assert not await second.accept(), "an unchanged stale snapshot must not overwrite accepted work"
        async with copies.create(str(source), "apply") as later:
            async with later.backend(backend).create(SandboxSpec()) as vm:
                assert await command(vm, "from pathlib import Path; print(Path('README.md').read_text(), end='')") == "first node\n"
            await later.accept()

    asyncio.run(run())


def test_patch_round_trip_preserves_host_git_and_ignores_guest_metadata(backend, git_repo):
    source = git_repo(commit=True)
    original_config = (source / ".git/config").read_bytes()
    original_index = (source / ".git/index").read_bytes()
    original_head = (source / ".git/refs/heads/main").read_bytes()
    (source / "remove.txt").write_text("delete me\n")
    (source / "program").write_text("old")
    (source / "data.bin").write_bytes(bytes(range(256)))
    (source / ".gitignore").write_text("ignored/\n")

    async def run():
        async with WorkspaceCopies().create(str(source), "apply") as copy:
            async with copy.backend(backend).create(SandboxSpec()) as vm:
                await command(vm, """from pathlib import Path
import subprocess
Path('README.md').write_text('accepted edit')
Path('remove.txt').unlink()
Path('program').write_text('new')
Path('program').chmod(0o755)
Path('data.bin').write_bytes(b'\\x00new binary')
Path('new file.txt').write_text('new file')
Path('ignored').mkdir()
Path('ignored/build').write_text('build output')
subprocess.run(['git', '-c', 'user.name=Guest', '-c', 'user.email=guest@example.test', 'add', '--all'], check=True)
subprocess.run(['git', '-c', 'user.name=Guest', '-c', 'user.email=guest@example.test', 'commit', '-qm', 'guest change'], check=True)
Path('.git/config').write_text('invalid guest config')
Path('.git/hooks').mkdir(exist_ok=True)
Path('.git/hooks/post-checkout').write_text('malicious')
""")
            patch = await copy.accept()
            assert b"GIT binary patch" in patch
    asyncio.run(run())
    assert not (source / "remove.txt").exists()
    assert (source / "program").read_text() == "new"
    assert (source / "program").stat().st_mode & stat.S_IXUSR
    assert (source / "data.bin").read_bytes() == b"\x00new binary"
    assert (source / "new file.txt").read_text() == "new file"
    assert not (source / "ignored").exists()
    assert (source / "README.md").read_text() == "accepted edit"
    assert (source / ".git/config").read_bytes() == original_config
    assert (source / ".git/index").read_bytes() == original_index
    assert (source / ".git/refs/heads/main").read_bytes() == original_head
    assert not (source / ".git/hooks/post-checkout").exists()


def test_review_copies_are_read_only_and_even_forced_edits_are_discarded(backend, git_repo):
    source = git_repo(commit=True)
    async def review(copies, name):
        async with copies.create(str(source), "read-only") as copy:
            async with copy.backend(backend).create(SandboxSpec()) as vm:
                # A guest can change its own permissions. This must never grant
                # authority to copy its changes back into the run branch.
                await command(vm, f"""from pathlib import Path
import stat
root = Path('.')
file = Path('README.md')
assert not (file.stat().st_mode & 0o222)
root.chmod(0o755)
file.chmod(0o644)
file.write_text({name!r})
Path('injected.txt').write_text('must not return')
""")
                await asyncio.sleep(0)
            assert not await copy.accept()
    async def run():
        copies = WorkspaceCopies()
        await asyncio.gather(review(copies, "facet one"), review(copies, "facet two"))
    asyncio.run(run())
    assert (source / "README.md").read_text() == "engine\n"
    assert not (source / "injected.txt").exists()
    node = ReviewNode(agent="codex", cwd=str(source), facet="correctness")
    assert node.workspace_policy == "read-only"


@pytest.mark.parametrize("backend", ["process"], indirect=True)
def test_conflicting_patch_does_not_partially_apply(backend, git_repo):
    source = git_repo(commit=True)
    async def run():
        async with WorkspaceCopies().create(str(source), "apply") as copy:
            async with copy.backend(backend).create(SandboxSpec()) as vm:
                await command(vm, "from pathlib import Path; Path('README.md').write_text('guest'); Path('another.txt').write_text('guest')")
                (source / "README.md").write_text("concurrent host change")
            with pytest.raises(WorkspacePatchConflict) as caught:
                await copy.accept()
            assert b"guest" in caught.value.patch
            assert base64.b64decode(caught.value.attachment()["content"]) == caught.value.patch
    asyncio.run(run())
    assert (source / "README.md").read_text() == "concurrent host change"
    assert not (source / "another.txt").exists()


async def run_graph(builder, backend):
    runtime = LangGraphRuntime(LangGraphDefinition(
        graph_id=GraphId("copy-test"), name="Copies",
        graph=builder.compile(checkpointer=InMemorySaver()),
    ), sandbox=backend)
    events = EventLog()
    runtime.observe(events.append)
    try:
        run = await runtime.start(GraphId("copy-test"), {})
        async with asyncio.timeout(180):
            async for event in events.stream(run.run_id):
                if event.kind.value in {"run.finished", "run.failed"}:
                    return event, events.since(run.run_id), await runtime.snapshot(run.run_id)
    finally:
        await runtime.aclose()


@pytest.fixture
def agent_workspace(git_repo):
    root = git_repo(commit=True)
    (root / "agent.py").write_text('''import json, pathlib, sys
for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if method == 'initialize':
        result = {'protocolVersion': 1, 'agentCapabilities': {}}
    elif method == 'session/new':
        result = {'sessionId': 'copy-session'}
    elif method == 'session/prompt':
        action = request['params']['prompt'][0]['text']
        file = pathlib.Path('README.md')
        if action == 'write':
            file.write_text('implementation output')
        elif action == 'fail':
            file.write_text('failed output')
            sys.exit(7)
        else:
            assert file.read_text() == 'implementation output'
            if action == 'review-write':
                pathlib.Path('.').chmod(0o755)
                file.chmod(0o644)
                file.write_text('review must not return')
        print(json.dumps({'jsonrpc':'2.0', 'method':'session/update', 'params':{'sessionId':'copy-session','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':action}}}}), flush=True)
        result = {'stopReason':'end_turn'}
    else:
        continue
    print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}), flush=True)
''')
    return root


def node(root, action, **kwargs):
    return ACPNode(agent="test", registry=ACPAgentRegistry([
        StdioACPProvider(name="test", command=("python3", "agent.py")),
    ]), cwd=str(root), prompt=action, **kwargs)


def test_runtime_copies_implementation_outputs_into_next_node(backend, agent_workspace):
    if isinstance(backend, ProcessSandbox):
        pytest.skip("ACP now requires enforced egress; process workspace copying is tested separately")
    builder = StateGraph(dict)
    builder.add_node("implementation", node(agent_workspace, "write"))
    builder.add_node("review", node(agent_workspace, "read", workspace_policy="read-only"))
    builder.add_edge(START, "implementation")
    builder.add_edge("implementation", "review")
    builder.add_edge("review", END)
    final, events, _ = asyncio.run(run_graph(builder, backend))
    assert final.kind.value == "run.finished", final.payload
    assert (agent_workspace / "README.md").read_text() == "implementation output"
    assert any(event.kind.value == "tool.result" and event.payload.get("name") == "apply_workspace_patch" for event in events)


def test_failed_node_discards_its_changes(backend, agent_workspace):
    if isinstance(backend, ProcessSandbox):
        pytest.skip("ACP now requires enforced egress; process workspace copying is tested separately")
    builder = StateGraph(dict)
    builder.add_node("implementation", node(agent_workspace, "fail"))
    builder.add_edge(START, "implementation")
    builder.add_edge("implementation", END)
    final, _, _ = asyncio.run(run_graph(builder, backend))
    assert final.kind.value == "run.failed"
    assert (agent_workspace / "README.md").read_text() == "engine\n"


def test_conflict_is_a_node_failure_with_attached_patch(backend, agent_workspace):
    if isinstance(backend, ProcessSandbox):
        pytest.skip("ACP now requires enforced egress; process workspace copying is tested separately")
    class ConflictingBackend:
        @asynccontextmanager
        async def create(self, spec):
            async with backend.create(spec) as vm:
                yield vm
                (agent_workspace / "README.md").write_text("host won")
    builder = StateGraph(dict)
    builder.add_node("implementation", node(agent_workspace, "write"))
    builder.add_edge(START, "implementation")
    builder.add_edge("implementation", END)
    final, _, _ = asyncio.run(run_graph(builder, ConflictingBackend()))
    assert final.kind.value == "run.failed"
    assert final.node_id == "implementation"
    assert "conflicts" in final.payload["error"]
    assert b"implementation output" in base64.b64decode(final.payload["patch"]["content"])
    assert (agent_workspace / "README.md").read_text() == "host won"


class ParallelState(TypedDict, total=False):
    implementation: str
    facet_a: str
    facet_b: str


def test_parallel_review_nodes_cannot_change_the_run_branch(backend, agent_workspace):
    if isinstance(backend, ProcessSandbox):
        pytest.skip("ACP now requires enforced egress; process workspace copying is tested separately")
    registry = ACPAgentRegistry([StdioACPProvider(name="test", command=("python3", "agent.py"))])
    builder = StateGraph(ParallelState)
    builder.add_node("implementation", node(agent_workspace, "write"))
    builder.add_edge(START, "implementation")
    for facet in ("facet_a", "facet_b"):
        builder.add_node(facet, ReviewNode(
            agent="test", registry=registry, cwd=str(agent_workspace),
            prompt="review-write", facet=facet, output_key=facet,
        ))
        builder.add_edge("implementation", facet)
        builder.add_edge(facet, END)
    final, _, snapshot = asyncio.run(run_graph(builder, backend))
    assert final.kind.value == "run.finished", final.payload
    assert snapshot.values["facet_a"] == "review-write"
    assert snapshot.values["facet_b"] == "review-write"
    assert (agent_workspace / "README.md").read_text() == "implementation output"


@pytest.mark.parametrize("backend", ["process"], indirect=True)
def test_parallel_nonconflicting_patches_both_survive(backend, git_repo):
    source = git_repo(commit=True)
    async def run():
        copies = WorkspaceCopies()
        async with copies.create(str(source), "apply") as first, copies.create(str(source), "apply") as second:
            async with first.backend(backend).create(SandboxSpec()) as one, second.backend(backend).create(SandboxSpec()) as two:
                await command(one, "from pathlib import Path; Path('one.txt').write_text('one')")
                await command(two, "from pathlib import Path; Path('two.txt').write_text('two')")
            await asyncio.gather(first.accept(), second.accept())
    asyncio.run(run())
    assert (source / "one.txt").read_text() == "one"
    assert (source / "two.txt").read_text() == "two"


@pytest.mark.parametrize("backend", ["process"], indirect=True)
def test_cancellation_discards_the_node_copy(backend, git_repo):
    source = git_repo(commit=True)
    async def run():
        async def cancelled():
            async with WorkspaceCopies().create(str(source), "apply") as copy:
                async with copy.backend(backend).create(SandboxSpec()) as vm:
                    await command(vm, "from pathlib import Path; Path('README.md').write_text('cancelled')")
                    raise asyncio.CancelledError
                await copy.accept()
        with pytest.raises(asyncio.CancelledError):
            await cancelled()
    asyncio.run(run())
    assert (source / "README.md").read_text() == "engine\n"


@pytest.mark.parametrize("target", ["../outside", ".git/config"])
def test_export_cannot_introduce_links_outside_the_tree_or_into_git(target, git_repo):
    source = git_repo(commit=True)
    async def run():
        async with WorkspaceCopies().create(str(source), "apply") as copy:
            async with copy.backend(ProcessSandbox()).create(SandboxSpec()) as vm:
                await command(vm, f"from pathlib import Path; Path('bad-link').symlink_to({target!r})")
            with pytest.raises(ValueError, match="escaping symlink"):
                await copy.accept()
    asyncio.run(run())
    assert not (source / "bad-link").is_symlink()


def test_worktree_pointer_becomes_independent_git_history(backend, git_repo, tmp_path):
    import subprocess

    repository = git_repo(commit=True)
    source = tmp_path / "run-worktree"
    subprocess.run(["git", "-C", str(repository), "worktree", "add", "-b", "agent/node-copy", str(source)], check=True, capture_output=True)
    assert (source / ".git").is_file()
    async def run():
        async with WorkspaceCopies().create(str(source), "apply") as copy:
            async with copy.backend(backend).create(SandboxSpec()) as vm:
                await command(vm, """from pathlib import Path
import subprocess
assert Path('.git').is_dir()
assert not Path('.git/objects/info/alternates').exists()
assert subprocess.check_output(['git', 'symbolic-ref', 'HEAD']).strip() == b'refs/heads/agent/node-copy'
assert subprocess.run(['git', 'config', '--get', 'remote.origin.url'], capture_output=True).returncode == 1
assert subprocess.check_output(['git', 'show', 'main:README.md']) == b'engine\\n'
Path('README.md').write_text('node output')
""")
            await copy.accept()
    asyncio.run(run())
    assert (source / "README.md").read_text() == "node output"
    assert (repository / "README.md").read_text() == "engine\n"
    assert (source / ".git").is_file()


def test_patch_can_replace_a_file_with_a_directory(git_repo):
    source = git_repo(commit=True)
    async def run():
        async with WorkspaceCopies().create(str(source), "apply") as copy:
            async with copy.backend(ProcessSandbox()).create(SandboxSpec()) as vm:
                await command(vm, "from pathlib import Path; Path('README.md').unlink(); Path('README.md').mkdir(); Path('README.md/nested').write_text('new layout')")
            await copy.accept()
    asyncio.run(run())
    assert (source / "README.md/nested").read_text() == "new layout"
