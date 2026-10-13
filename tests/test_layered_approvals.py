"""The approval ceiling, repository defaults, and immutable run snapshots."""
import asyncio
import json
from dataclasses import replace
import subprocess

import pytest

from engine.domain import ApprovalKind
from engine.ports.permissions import ApprovalCapability as Capability
from engine.runtime.config import ApprovalConfig, BashApprovalConfig, parse_engine_config
from engine.runtime.layered_approvals import PolicyError, read_repository_policy, resolve_policy
from engine.graph_runtime_langgraph.runtime import _configured_decision
from engine.runtime.approval_policy import PolicyDecision


BACKEND = ApprovalConfig(
    auto_approve=True, allow=(Capability.READ, Capability.EDIT),
    bash=BashApprovalConfig(allow=("uv run pytest **",), deny=("rm **",)),
)


def test_repository_is_clamped_and_every_value_has_provenance():
    resolved = resolve_policy(BACKEND, {
        "allow": ["read", "bash"],
        "bash": {"allow": ["uv run pytest **", "curl **"], "deny": ["sudo **"]},
    })
    assert resolved["allow"] == ["read"]
    assert resolved["bash"]["allow"] == ["uv run pytest **"]
    assert resolved["bash"]["deny"] == ["rm **", "sudo **"]
    assert len(resolved["warnings"]) == 2
    assert resolved["provenance"] == {
        "auto_approve": "repository", "allow": "repository ∩ backend",
        "bash.allow": "repository ∩ backend", "bash.ask": "backend",
        "bash.deny": "backend ∩ repository",
    }
    assert resolved["auto_approve"] is False


def test_no_file_uses_backend_values():
    policy = resolve_policy(BACKEND)
    assert policy["auto_approve"] is True
    assert policy["allow"] == ["read", "edit"]
    assert set(policy["provenance"].values()) == {"backend"}
    assert policy["warnings"] == []


def test_run_can_tighten_and_cannot_loosen():
    policy = resolve_policy(BACKEND, overrides={"auto_approve": False, "allow": ["read"]})
    assert policy["auto_approve"] is False
    assert policy["provenance"]["auto_approve"] == "run"
    for override in (
        {"auto_approve": True}, {"allow": ["read", "edit", "web"]},
        {"bash": {"allow": ["curl **"]}}, {"bash": {"deny": []}},
    ):
        with pytest.raises(PolicyError, match="run"):
            resolve_policy(BACKEND, {"auto_approve": False}, override)


def test_trusted_alias_only_delegates_within_ceiling():
    backend = replace(BACKEND, auto_approve=False, repositories=())
    resolved = resolve_policy(backend, {"auto_approve": True}, repository_allowed=False, trusted=True)
    assert resolved["auto_approve"] is False
    assert any("deprecated" in warning for warning in resolved["warnings"])
    assert any("clamped" in warning for warning in resolved["warnings"])
    denied = resolve_policy(BACKEND, {"auto_approve": False}, repository_allowed=False)
    assert denied["auto_approve"] is True
    assert "does not delegate" in denied["warnings"][0]
    assert parse_engine_config({"approvals": {"repositories": ["acme/*"]}}).approvals.repositories == ("acme/*",)


def test_unattended_denies_unknown_requests_and_backend_denies_win():
    policy = resolve_policy(BACKEND, overrides={"auto_approve": False}, reject_instead_of_ask=True)
    assert _configured_decision(policy, ApprovalKind.TOOL_USE, "", "unknown") is PolicyDecision.DENY
    assert _configured_decision(policy, ApprovalKind.TOOL_USE, "", "read") is PolicyDecision.ALLOW
    assert _configured_decision(policy, ApprovalKind.COMMAND_EXECUTION, "rm file", "") is PolicyDecision.DENY
    assert _configured_decision(policy, ApprovalKind.USER_INPUT, "", "") is PolicyDecision.DENY


@pytest.mark.parametrize("document", [{"auto_approve": "yes"}, {"allow": ["typo"]}, {"spend": 10}, []])
def test_malformed_policy_is_refused(document):
    with pytest.raises(PolicyError):
        resolve_policy(BACKEND, document)


def test_repository_policy_comes_from_base_commit_not_working_tree(git_repo):
    root = git_repo(commit=True)
    policy = root / ".openengine.toml"
    policy.write_text('[approvals]\nauto_approve = false\n')
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()
    git("add", ".openengine.toml")
    git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "policy")
    base = git("rev-parse", "HEAD")
    policy.write_text('[approvals]\nauto_approve = true\n')
    git("add", ".openengine.toml")
    git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "looser")
    policy.write_text('invalid toml !')
    nested = root / "nested"
    nested.mkdir()
    document, revision = asyncio.run(read_repository_policy(str(nested), base))
    assert revision == base and document == {"auto_approve": False}
    absent, _ = asyncio.run(read_repository_policy(str(root), f"{base}^"))
    assert absent is None


def test_two_repositories_share_a_daemon_but_have_different_approvals(tmp_path, git_repo):
    from engine.graph_runtime import GraphId, NodeId, RunNotSteerableError
    from engine.graph_runtime_langgraph import LangGraphDefinition, LangGraphRuntime, current_execution
    from engine.graph_runtime_langgraph.store import SqliteGraphRuntimeStore
    from engine.graph_runtime_langgraph.workflows import State
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import START, END, StateGraph

    roots = {}
    for name, enabled in (("relaxed", True), ("strict", False)):
        root = git_repo(path=tmp_path / name, commit=True)
        (root / ".openengine.toml").write_text(f"[approvals]\nauto_approve = {str(enabled).lower()}\n")
        subprocess.run(["git", "-C", str(root), "add", ".openengine.toml"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                        "commit", "-m", "policy"], check=True, capture_output=True)
        roots[name] = str(root)

    async def scenario():
        async def work(_state):
            await current_execution().ask(reason="command", command="echo hi")
            return {}
        builder = StateGraph(State)
        builder.add_node("work", work)
        builder.add_edge(START, "work")
        builder.add_edge("work", END)
        store = SqliteGraphRuntimeStore(tmp_path / "runs.sqlite3")
        runtime = LangGraphRuntime(LangGraphDefinition(GraphId("g"), "Graph", builder.compile(checkpointer=InMemorySaver())),
                                   store=store, approval_policy=BACKEND)
        try:
            relaxed = await runtime.start(GraphId("g"), {"repository": roots["relaxed"]})
            strict = await runtime.start(GraphId("g"), {"repository": roots["strict"]})
            async with asyncio.timeout(5):
                while True:
                    a, b = await runtime.snapshot(relaxed.run_id), await runtime.snapshot(strict.run_id)
                    if a.status.value == "completed" and b.pending_approvals:
                        break
                    await asyncio.sleep(.01)
            assert a.approval_policy["auto_approve"] is True
            assert b.approval_policy["auto_approve"] is False
            with pytest.raises(RunNotSteerableError, match="exceeds"):
                await runtime.set_auto_approve(strict.run_id, NodeId("work"), True)
            with pytest.raises(PolicyError, match="only tighten"):
                await runtime.start(GraphId("g"), {"repository": roots["strict"], "approvals": {"auto_approve": True}})
            # An agent's subsequent file edits cannot replace the run record.
            (tmp_path / "strict" / ".openengine.toml").write_text("[approvals]\nauto_approve = true\n")
            assert (await runtime.snapshot(strict.run_id)).approval_policy == b.approval_policy
            reopened = SqliteGraphRuntimeStore(tmp_path / "runs.sqlite3")
            assert (await reopened.run(strict.run_id)).approval_policy == b.approval_policy
            reopened.close()
        finally:
            await runtime.aclose()
            store.close()
    asyncio.run(scenario())


def test_provisioned_policy_is_finalized_once_and_unattended_never_waits(tmp_path, monkeypatch):
    from engine.domain import ApprovalDecision, ApprovalId
    from engine.graph_runtime import ApprovalNotPendingError
    from engine.graph_runtime import GraphId
    from engine.graph_runtime_langgraph import LangGraphDefinition, LangGraphRuntime, current_execution
    from engine.graph_runtime_langgraph.workflows import State
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import START, END, StateGraph

    async def read(repository, ref="HEAD", **kwargs):
        return {"auto_approve": repository != "checkout"}, repository
    monkeypatch.setattr("engine.graph_runtime_langgraph.runtime.read_repository_policy", read)

    async def scenario():
        async def work(_state):
            decision = await current_execution().ask(reason="unlisted command", command="unknown-command")
            assert decision is ApprovalDecision.CANCEL
            return {"refused": True}
        builder = StateGraph(State)
        builder.add_node("work", work)
        builder.add_edge(START, "work")
        builder.add_edge("work", END)
        runtime = LangGraphRuntime(LangGraphDefinition(GraphId("g"), "Graph", builder.compile(checkpointer=InMemorySaver())),
                                   approval_policy=BACKEND)
        async def observe(event):
            if event.payload.get("policyRejected"):
                with pytest.raises(ApprovalNotPendingError):
                    await runtime.decide(event.run_id, ApprovalId(event.payload["approvalId"]), ApprovalDecision.ACCEPT)
        runtime.observe(observe)
        try:
            from engine.domain import RunId
            from engine.graph_runtime.metadata import RunMetadata
            from engine.graph_runtime_langgraph.store import RunRecord
            scheduled = RunId("scheduled")
            await runtime.store.remember_run(RunRecord(
                scheduled, GraphId("g"), metadata=RunMetadata(scheduled=True),
                approval_policy={"overrides": {"allow": []}, "reject_instead_of_ask": True},
            ))
            run = await runtime.start(GraphId("g"), {"repository": "source"}, run_id=scheduled)
            await runtime.resolve_workspace_policy(run.run_id, "checkout")
            final = (await runtime.store.run(run.run_id)).approval_policy
            assert final["base_revision"] == "checkout"
            assert final["workspace_resolved"] is True
            assert final["allow"] == [] and final["reject_instead_of_ask"] is True
            await runtime.resolve_workspace_policy(run.run_id, "agent-edited-checkout")
            assert (await runtime.store.run(run.run_id)).approval_policy == final
            async with asyncio.timeout(5):
                while (snapshot := await runtime.snapshot(run.run_id)).status.value != "completed":
                    await asyncio.sleep(.01)
            assert snapshot.values["refused"] is True
            assert not snapshot.pending_approvals
        finally:
            await runtime.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("pretty", [False, True])
def test_policy_show_prints_provenance_and_clamping(monkeypatch, capsys, pretty):
    from engine.apps.cli.__main__ import parser
    from engine.cli import commands
    from engine.cli.backends import Backend
    class Client:
        def get(self, path, **query):
            assert path == "/policy" and query == {"repo": "acme/repo"}
            return resolve_policy(ApprovalConfig(), {"auto_approve": True})
    backend = Backend(name="test", url="https://example.test")
    monkeypatch.setattr(commands, "_connect", lambda args: (backend, Client()))
    args = parser().parse_args(["policy", "show", "--repo", "acme/repo", "--backend", "test"]
                               + (["--pretty"] if pretty else []))
    assert commands.policy_show(args) == 0
    out = capsys.readouterr().out
    if pretty:
        assert 'auto_approve = false  [repository ∩ backend]' in out
        assert 'clamped to backend ceiling' in out
    else:
        assert json.loads(out) == resolve_policy(ApprovalConfig(), {"auto_approve": True})


def test_narrow_shell_patterns_are_allowed_without_globbing_globs():
    narrowed = resolve_policy(BACKEND, overrides={"bash": {"allow": ["uv run pytest tests/**"]}})
    assert narrowed["bash"]["allow"] == ["uv run pytest tests/**"]
    assert narrowed["auto_approve"] is False
    tricky = ApprovalConfig(bash=BashApprovalConfig(allow=("echo [ab]*",)))
    with pytest.raises(PolicyError):
        resolve_policy(tricky, overrides={"bash": {"allow": ["echo [a-z]*"]}})


def test_unattended_mode_disables_blanket_consent_and_refuses_an_override():
    policy = resolve_policy(BACKEND, reject_instead_of_ask=True)
    assert policy["auto_approve"] is False
    with pytest.raises(PolicyError, match="unattended"):
        resolve_policy(BACKEND, overrides={"auto_approve": True}, reject_instead_of_ask=True)


def test_shell_denies_do_not_reclassify_mcp_git_as_a_shell_command():
    backend = ApprovalConfig(allow=(Capability.MCP,), bash=BashApprovalConfig(deny=("git push **",)))
    policy = resolve_policy(backend)
    assert _configured_decision(policy, ApprovalKind.TOOL_USE, "git push origin feature", "mcp__workflow__git_subcommand") is PolicyDecision.ALLOW
    assert _configured_decision(policy, ApprovalKind.COMMAND_EXECUTION, "git push origin feature", "execute") is PolicyDecision.DENY
