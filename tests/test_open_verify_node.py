import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from engine.graph_runtime_langgraph.components import OpenVerify
from test_graph_workflow_definitions import definition_module
from test_verification_publication import HEAD, PR, bundle, collaborators


def test_node_runs_cli_consumes_bundle_and_posts_evidence(tmp_path, monkeypatch):
    source, uploader = collaborators()
    source.view_change_request.return_value.base_ref = "main"
    execution = SimpleNamespace(
        runtime=SimpleNamespace(source_control=source), say=AsyncMock()
    )
    monkeypatch.setattr(
        "engine.graph_runtime_langgraph.components.open_verify.current_execution",
        lambda: execution,
    )
    fixture = bundle(tmp_path / "fixture")
    script = tmp_path / "ov_fixture.py"
    script.write_text(
        "import sys, shutil, pathlib\n"
        "args = sys.argv\n"
        f'assert args[args.index("--head")+1] == {HEAD!r}\n'
        'assert args[args.index("--base")+1] == "origin/main"\n'
        'dest = pathlib.Path(args[args.index("--output")+1]) / "run"\n'
        f"shutil.copytree({str(fixture.parent)!r}, dest)\n"
    )
    node = OpenVerify(
        uploader=uploader,
        output_directory=tmp_path / "output",
        command=(sys.executable, str(script)),
    )
    state = {
        "workspaceId": "ws-test",
        "pr_url": PR,
        "workspace": str(tmp_path),
        "task": "Check login",
    }
    result = asyncio.run(node(state))["verification"]
    assert result["status"] == "passed"
    assert source.add_comment.await_count == 1
    assert len(result["artifacts"]) == 3


def test_workflow_opt_in_runs_verification_before_human_review(tmp_path):
    _, uploader = collaborators()
    module = definition_module()
    node = OpenVerify(uploader=uploader, output_directory=tmp_path)
    graph = module.pipeline("codex", verification=node)
    edges = {(edge.source, edge.target) for edge in graph.compile().get_graph().edges}
    assert ("impact-analysis", "verification") in edges
    assert ("verification", "human-review") in edges
    assert ("impact-analysis", "human-review") not in edges
    default_edges = {
        (edge.source, edge.target)
        for edge in module.pipeline("codex").compile().get_graph().edges
    }
    assert ("impact-analysis", "human-review") in default_edges


def test_node_timeout_cleans_up_child_without_publication(tmp_path, monkeypatch):
    source, uploader = collaborators()
    source.view_change_request.return_value.base_ref = "main"
    execution = SimpleNamespace(
        runtime=SimpleNamespace(source_control=source), say=AsyncMock()
    )
    monkeypatch.setattr(
        "engine.graph_runtime_langgraph.components.open_verify.current_execution",
        lambda: execution,
    )
    processes = []
    create = asyncio.create_subprocess_exec

    async def record_process(*args, **kwargs):
        process = await create(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record_process)
    node = OpenVerify(
        uploader=uploader,
        output_directory=tmp_path / "output",
        timeout=0.1,
        command=(sys.executable, "-c", "import time; time.sleep(60)"),
    )
    with pytest.raises(TimeoutError):
        asyncio.run(
            node({"workspaceId": "ws-test", "pr_url": PR, "workspace": str(tmp_path)})
        )
    assert processes[0].returncode is not None
    uploader.upload.assert_not_called()
    source.add_comment.assert_not_called()
