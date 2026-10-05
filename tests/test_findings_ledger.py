"""The review ledger survives retries and cannot change review decisions."""
import asyncio
from dataclasses import replace
import json
import sqlite3
from types import SimpleNamespace

from alembic import command
import pytest

from engine.adapters.state_store.sqlite import SQLiteStateStore
from engine.domain import ApprovalDecision
from engine.domain.findings_ledger import ReviewFinding
from engine.graph_runtime_langgraph.components import findings as nodes
from engine.ports.source_control import CommentResult
from engine.runtime.findings import capture, changed_near_line, reconcile
from migrations.migration import alembic_config, main as migrate
from test_review_findings import completed

PR = "https://github.com/o/r/pull/1"
FINDING = dict(tagline="Work is lost", description="Saving overwrites work.",
               file="save.py", line=10, agent="claude", facet="bugs", severity="high")


@pytest.fixture
def store():
    value = SQLiteStateStore(":memory:")
    yield value
    value.close()


def outcomes(store):
    return [dict(row) for row in store._connection.execute("SELECT * FROM finding_outcomes")]


@pytest.mark.parametrize("existing", [False, True])
def test_migrate_fresh_existing_and_downgrade(tmp_path, existing):
    path = tmp_path / "state.db"
    url = f"sqlite:///{path}"
    if existing:
        migrate([url, "--revision", "d487944fbd15"])
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO run_states (run_id, state_json) VALUES ('old', '{}')")
    migrate([url])
    with sqlite3.connect(path) as db:
        assert {row[1] for row in db.execute("PRAGMA table_info(review_findings)")} >= {"head_sha", "comment_id", "model_tier"}
        assert len(list(db.execute("PRAGMA index_list(review_findings)"))) == 4
        assert list(db.execute("PRAGMA foreign_key_list(finding_outcomes)"))
        if existing:
            assert db.execute("SELECT run_id FROM run_states").fetchone() == ("old",)
    command.downgrade(alembic_config(url), "d487944fbd15")
    with sqlite3.connect(path) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name IN ('review_findings', 'finding_outcomes')").fetchall()
    migrate([url])


def test_ledger_identity_lineage_comment_and_idempotent_outcome(store):
    row = ReviewFinding(run_id="run", node_id="review", stage="posted", **FINDING, model_tier="elevated")
    capture(store.findings, [row])
    capture(store.findings, [replace(row, comment_id=123, pr_url=PR, head_sha="head")])
    capture(store.findings, [replace(row, comment_id=123, pr_url=PR, head_sha="head")])
    found, = store.findings.list_findings(pr_url=PR, run_id="run")
    assert found.id == row.id
    assert (found.comment_id, found.head_sha, found.model_tier, found.agent, found.facet) == (123, "head", "elevated", "claude", "bugs")
    assert len(outcomes(store)) == 1
    assert outcomes(store)[0]["signal"] == "comment_posted"
    assert not store.findings.list_findings(run_id="other")
    with pytest.raises(sqlite3.IntegrityError):
        store.findings.record_outcome("unknown", "free_form_signal", source="test")


def test_terminal_stages_capture_and_preserve_lineage(store, monkeypatch):
    execution = SimpleNamespace(run_id="run", node_id="review-bugs", runtime=SimpleNamespace(findings_ledger=store.findings))
    monkeypatch.setattr(nodes, "current_execution", lambda: execution)
    review = nodes.ReviewNode(agent="claude", facet="bugs", cwd="/tmp", output_key="bugs")
    result = review._terminal_update(completed([{**FINDING, "agent": "spoof", "facet": "spoof"}]))
    execution.node_id = "reranker"
    reranker = nodes.RerankerNode(agent="codex", cwd="/tmp", output_key="review")
    assert reranker._terminal_update(completed(result["bugs"])) == {"review": [FINDING]}
    rows = store.findings.list_findings(run_id="run")
    assert {(r.stage, r.node_id, r.agent, r.facet) for r in rows} == {
        ("raw", "review-bugs", "claude", "bugs"), ("reranked", "reranker", "claude", "bugs"),
    }


@pytest.mark.parametrize("decision,note,selected", [
    (ApprovalDecision.ACCEPT, "", 2),
    (ApprovalDecision.ACCEPT, json.dumps([FINDING]), 1),
    (ApprovalDecision.CANCEL, json.dumps([FINDING]), 0),
])
def test_triage_records_every_reranked_finding(store, monkeypatch, decision, note, selected):
    async def say(*args, **kwargs): pass
    async def ask(**kwargs): return decision
    execution = SimpleNamespace(run_id="run", node_id="triage", runtime=SimpleNamespace(findings_ledger=store.findings),
                                say=say, ask=ask, pending_messages=lambda: [note])
    monkeypatch.setattr(nodes, "current_execution", lambda: execution)
    findings = [FINDING, {**FINDING, "tagline": "Another bug"}]
    result = asyncio.run(nodes.TriageNode(findings_key="review")({"review": findings}))
    assert len(result["fix"]) == selected
    signals = outcomes(store)
    assert sum(row["signal"] == "triage_selected" for row in signals) == selected
    assert sum(row["signal"] == "triage_rejected" for row in signals) == 2 - selected
    assert len([r for r in store.findings.list_findings() if r.stage == "triaged"]) == selected


def test_failed_ledger_does_not_break_terminal_or_triage(monkeypatch, caplog):
    class Broken:
        def record_findings(self, findings): raise RuntimeError("disk full")
    async def say(*args, **kwargs): pass
    async def ask(**kwargs): return ApprovalDecision.ACCEPT
    execution = SimpleNamespace(run_id="run", node_id="review", runtime=SimpleNamespace(findings_ledger=Broken()),
                                say=say, ask=ask, pending_messages=lambda: [])
    monkeypatch.setattr(nodes, "current_execution", lambda: execution)
    assert nodes.ReviewNode(agent="claude", facet="bugs", cwd="/tmp", output_key="review")._terminal_update(completed([FINDING])) == {"review": [FINDING]}
    assert nodes.RerankerNode(agent="codex", cwd="/tmp", output_key="review")._terminal_update(completed([FINDING])) == {"review": [FINDING]}
    assert asyncio.run(nodes.TriageNode(findings_key="review")({"review": [FINDING]})) == {"fix": [FINDING]}
    assert "disk full" in caplog.text


@pytest.mark.parametrize("line,expected", [(6, False), (7, True), (10, True), (13, True), (14, False)])
def test_changed_line_window(line, expected):
    before = [f"line {i}" for i in range(1, 25)]
    after = before.copy()
    after[9] = "fixed"
    assert changed_near_line("\n".join(before), "\n".join(after), line) is expected


def test_reconciliation_coordinates_failures_and_idempotence(store):
    before = "\n".join(f"line {i}" for i in range(1, 30))
    after = before.replace("line 10\n", "fixed\n")
    for line in (10, 25):
        store.findings.record_findings([ReviewFinding(run_id="run", node_id="post", stage="posted", **{**FINDING, "line": line}, pr_url=PR, head_sha="head", comment_id=line)])
    async def read(sha, path): return before if sha == "head" else after
    assert asyncio.run(reconcile(store.findings, PR, "merge", read)) == 2
    assert asyncio.run(reconcile(store.findings, PR, "merge", read)) == 2
    assert {r["signal"] for r in outcomes(store)} == {"fixed_before_merge", "unchanged_at_merge"}
    assert all(r["value"] == "merge" for r in outcomes(store))
    assert changed_near_line(before, "new\n" + before, 25) is False
    assert changed_near_line(before, "", 10) is True
    assert changed_near_line(before, before.replace("line 10\n", ""), 10) is True
    async def failed(sha, path): raise RuntimeError("unavailable")
    assert asyncio.run(reconcile(store.findings, PR, "other", failed)) == 0
    assert len(outcomes(store)) == 2


@pytest.mark.parametrize("inline_success", [True, False])
def test_cli_returns_inline_and_fallback_ids(monkeypatch, inline_success):
    from engine.apps.cli import __main__ as cli
    monkeypatch.setattr(cli, "gh_json", lambda *args: {"headRefOid": "head"})
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        if "/pulls/" in args[6] and not inline_success:
            return SimpleNamespace(returncode=1, stderr="not in diff", stdout="")
        return SimpleNamespace(returncode=0, stdout='{"id": 123}', stderr="")
    monkeypatch.setattr(cli.subprocess, "run", run)
    result = cli.post_findings(PR, [FINDING])
    assert not result
    assert result.comments == [{"finding": FINDING, "comment_id": 123, "pr_url": PR, "head_sha": "head"}]
    assert len(calls) == (1 if inline_success else 2)
    if not inline_success:
        assert calls[-1][6] == "repos/o/r/issues/1/comments"


@pytest.mark.parametrize("inline", [True, False])
@pytest.mark.parametrize("broken", [False, True])
def test_reranker_posting_captures_immediately_with_lineage(store, monkeypatch, inline, broken):
    from unittest.mock import AsyncMock
    from engine.graph_runtime_langgraph import terminal_mcp
    from engine.runtime.terminal_mcp import TerminalMcpBroker
    from tests.test_terminal_mcp import _request

    brokers = []
    def broker_factory(**kwargs):
        broker = TerminalMcpBroker(**kwargs)
        brokers.append(broker)
        return broker
    monkeypatch.setattr(terminal_mcp, "TerminalMcpBroker", broker_factory)
    if broken:
        monkeypatch.setattr(store.findings, "record_findings", lambda rows: (_ for _ in ()).throw(RuntimeError("disk full")))
    async def scenario():
        source = SimpleNamespace(add_comment=AsyncMock(return_value=CommentResult(123, PR + "#issuecomment-123", "head" if inline else None)),
                                 finding_head=AsyncMock(return_value="head"))
        graph_store = SimpleNamespace(pull_requests=AsyncMock(return_value=(("o/r", 1),)), remember_comment=AsyncMock())
        execution = SimpleNamespace(run_id="run", node_id="reranker", execution_id="execution", runtime=SimpleNamespace(
            source_control=source, store=graph_store, findings_ledger=store.findings))
        binding = terminal_mcp.TerminalMcpServer(step_id="reranker", agent_id="codex", repository_tools=("add_comment",))
        async with binding({"workspaceId": "workspace"}, execution, None):
            args = {"pr_url": PR, "comment": "**Work is lost**\n\nSaving overwrites work.", "finding": FINDING}
            if inline:
                args.update(file="save.py", line=10)
            result = await brokers[0]._submit(_request(brokers[0], "post", "add_comment", args))
            assert result["ok"]
            assert json.loads(result["output"])["id"] == 123
            source.add_comment.assert_awaited_once()
            graph_store.remember_comment.assert_awaited_once()
        rows = store.findings.list_findings(run_id="run")
        if broken:
            assert rows == []
        else:
            row, = rows
            assert (row.stage, row.agent, row.facet, row.node_id, row.head_sha, row.comment_id, row.pr_url, row.file, row.line) == (
                "posted", "claude", "bugs", "reranker", "head", 123, PR, "save.py", 10)
            assert outcomes(store)[0]["signal"] == "comment_posted"
    asyncio.run(scenario())


def test_github_reads_exact_revision_and_distinguishes_deleted_file(monkeypatch):
    import base64
    from engine.adapters.source_control.github import GitHubSourceControl
    source = GitHubSourceControl("")
    calls = []
    async def api(method, path, **kwargs):
        calls.append(path)
        if "/git/commits/" in path:
            return {"tree": {"sha": "tree"}}
        if path.endswith("/git/trees/tree"):
            return {"tree": [{"path": "save.py", "type": "blob", "sha": "blob"}]}
        if path.endswith("/git/blobs/blob"):
            return {"encoding": "base64", "content": base64.b64encode(b"reviewed contents").decode()}
        raise AssertionError(path)
    monkeypatch.setattr(source, "_api", api)
    assert asyncio.run(source.finding_file(PR, "head", "save.py")) == "reviewed contents"
    assert calls[0].endswith("/git/commits/head")
    assert asyncio.run(source.finding_file(PR, "merge", "deleted.py")) == ""
    async def failed(*args, **kwargs): raise RuntimeError("forbidden")
    monkeypatch.setattr(source, "_api", failed)
    with pytest.raises(RuntimeError, match="forbidden"):
        asyncio.run(source.finding_file(PR, "merge", "save.py"))


@pytest.mark.parametrize("automated", [False, True])
def test_ingress_reconciles_all_merges_without_approving_bot_work(automated):
    from engine.apps.web.github_ingress import GithubIngress
    from test_github_ingress import _merged_pull_request
    async def scenario():
        reconciled, approved = [], []
        async def reconcile_merge(merge): reconciled.append(merge)
        async def approve(merge): approved.append(merge)
        ingress = GithubIngress(repository="acme/api", webhook_secret=lambda: "secret", handle_merge=approve, reconcile_merge=reconcile_merge)
        payload = _merged_pull_request(merge_commit_sha="merge", merged_by={"login": "user", "type": "Bot" if automated else "User"})
        assert ingress.accept("pull_request", payload)
        assert ingress.accept("pull_request", payload)
        await ingress.drain()
        await ingress.close()
        assert len(reconciled) == 1
        assert reconciled[0].merge_sha == "merge"
        assert len(approved) == (0 if automated else 1)
    asyncio.run(scenario())


def test_cli_post_record_endpoint_uses_durable_lineage_and_checks_pr(store):
    import httpx
    from engine.domain import RunId, RunState, TaskId, WorkflowId
    from test_web_app import _workflow_app, ConcurrentRunner

    async def scenario():
        await store.save(RunState(run_id=RunId("run"), task_id=TaskId("task"), workflow_id=WorkflowId("review"), inputs={"pr_url": PR}))
        store.findings.record_findings([ReviewFinding(run_id="run", node_id="reranker", stage="reranked", **FINDING)])
        app = _workflow_app(store, ConcurrentRunner())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
            body = {"finding": {**FINDING, "agent": "spoofed"}, "comment_id": 123, "pr_url": PR, "head_sha": "head"}
            response = await client.post("/api/runs/run/findings/posted", json=body)
            assert response.status_code == 200
            assert response.json() == {"recorded": True}
            response = await client.post("/api/runs/run/findings/posted", json={**body, "pr_url": PR + "9"})
            assert response.status_code == 400
            assert (await client.post("/api/runs/unknown/findings/posted", json=body)).status_code == 404
        row, = store.findings.list_findings(pr_url=PR)
        assert (row.agent, row.facet, row.comment_id, row.head_sha) == ("claude", "bugs", 123, "head")
    asyncio.run(scenario())


def test_records_survive_reopening(tmp_path):
    path = tmp_path / "state.db"
    store = SQLiteStateStore(path)
    row = ReviewFinding(run_id="run", node_id="reranker", stage="posted", pr_url=PR, head_sha="head", comment_id=123, **FINDING)
    capture(store.findings, [row])
    store.close()
    reopened = SQLiteStateStore(path)
    try:
        assert reopened.findings.list_findings(pr_url=PR) == [row]
        capture(reopened.findings, [row])
        assert len(outcomes(reopened)) == 1
    finally:
        reopened.close()


def test_reconciliation_error_cannot_block_merge_approval():
    from engine.apps.web.github_ingress import GithubIngress
    from test_github_ingress import _merged_pull_request
    async def scenario():
        approved = []
        async def broken(merge): raise RuntimeError("ledger unavailable")
        async def approve(merge): approved.append(merge)
        ingress = GithubIngress(repository="acme/api", webhook_secret=lambda: "secret", handle_merge=approve, reconcile_merge=broken)
        assert ingress.accept("pull_request", _merged_pull_request())
        await ingress.drain()
        await ingress.close()
        assert len(approved) == 1
    asyncio.run(scenario())
