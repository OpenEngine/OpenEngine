"""Exercise the runner contracts without an ACP provider or a local engine."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_workflow import action, finding, plan

from open_verify.artifacts import Artifacts
from open_verify.engine import ActionDispatcher, ToolSpec
from open_verify.models import Contract, Decision
from open_verify.runner import VerificationRunner


class Arguments(Contract):
    message: str


class FixtureEngine:
    """A different engine implementation, with no browser/process/provider internals."""

    allow_exec = True

    def __init__(self, project, artifacts):
        self.project = project
        self.artifacts = artifacts
        self.calls = []
        self.dispatcher = ActionDispatcher({
            "probe": ToolSpec(Arguments, "Read fixture state", self.probe),
        }, artifacts)

    async def probe(self, args):
        self.calls.append(args.message)
        return {"output": args.message}

    def catalog(self, stage):
        return self.dispatcher.catalog(stage)

    def environment(self):
        return {"managed_processes": []}

    def create_authentication(self, *, progress):
        return SimpleNamespace(state=None)

    def check_url(self, url):
        raise ValueError("This engine has no URL capability")

    async def execute(self, name, arguments, *, stage="execute"):
        return await self.dispatcher.execute(name, arguments, stage=stage)

    async def close(self):
        return []


class FixtureExecutor:
    """A scripted executor using the public context, without prompt or provider code."""

    def __init__(self, decisions):
        self.decisions = iter(decisions)
        self.contexts = []

    async def decide(self, context):
        self.contexts.append(context)
        return Decision.model_validate(next(self.decisions))


def components(tmp_path, executor, **options):
    artifacts = Artifacts(tmp_path / "runs")
    engine = FixtureEngine(tmp_path, artifacts)
    runner = VerificationRunner(None, engine, artifacts, executor=executor,
                                progress=lambda _: None, **options)
    return runner, engine, artifacts


def test_custom_executor_and_engine_complete_a_case_without_provider(tmp_path):
    executor = FixtureExecutor([plan(), action("probe", message="hello"), finding(evidence=["E0001"])])
    runner, engine, artifacts = components(tmp_path, executor)
    report = asyncio.run(runner.run("Check greeting"))
    assert report["status"] == "complete"
    assert report["steps"] == 3
    assert report["findings"][0]["status"] == "passed"
    assert engine.calls == ["hello"]
    assert [c.scope for c in executor.contexts] == ["discover", "case:greeting", "case:greeting"]
    assert json.loads((artifacts.path / "manifest.json").read_text())["status"] == "passed"


def test_executor_cannot_mutate_live_state_or_evidence(tmp_path):
    class MutatingExecutor(FixtureExecutor):
        async def decide(self, context):
            result = await super().decide(context)
            context.payload["state"]["request"] = "changed request"
            context.payload["state"]["status"] = "complete"
            if context.payload["state"].get("plan"):
                context.payload["state"]["plan"]["cases"][0]["checks"] = ["weakened"]
            if context.payload["recent_evidence"]:
                context.payload["recent_evidence"][0]["result"]["output"] = "forged"
            return result

    executor = MutatingExecutor([plan(), action("probe", message="hello"), finding(evidence=["E0001"])])
    runner, _, artifacts = components(tmp_path, executor)
    report = asyncio.run(runner.run("Check greeting"))
    assert report["request"] == "Check greeting"
    assert report["steps"] == 3
    assert report["plan"]["cases"][0]["checks"] == ["Prints hello and exits 0"]
    assert artifacts.observations[0]["result"]["output"] == "hello"


@pytest.mark.parametrize(("name", "arguments", "stage"), [
    ("unknown", {"message": "hello"}, "execute"),
    ("probe", {"message": "hello", "undeclared": True}, "execute"),
    ("probe", {"message": "hello"}, "discover"),
    ("probe", {"message": "hello"}, "invented"),
])
def test_dispatch_refuses_before_side_effects(tmp_path, name, arguments, stage):
    artifacts = Artifacts(tmp_path / "runs")
    engine = FixtureEngine(tmp_path, artifacts)
    result = asyncio.run(engine.execute(name, arguments, stage=stage))
    assert not result["ok"]
    assert result["id"] == "E0001"
    assert not engine.calls
    assert engine.catalog("discover") == {}
    assert "probe" in engine.catalog("execute")


def test_refused_decisions_consume_budget_without_extra_execution(tmp_path):
    executor = FixtureExecutor([plan(), action("unknown")])
    runner, engine, _ = components(tmp_path, executor, max_steps=2)
    report = asyncio.run(runner.run("Check greeting"))
    assert report["status"] == "incomplete"
    assert report["steps"] == 2
    assert report["findings"][0]["status"] == "inconclusive"
    assert len(executor.contexts) == 2
    assert not engine.calls


def test_cancelled_action_preserves_plan_and_latest_step_without_restarting(tmp_path):
    executor = FixtureExecutor([plan(), action("probe", message="hello")])
    runner, engine, artifacts = components(tmp_path, executor)

    async def cancel(_):
        raise asyncio.CancelledError()

    engine.dispatcher = ActionDispatcher({"probe": ToolSpec(Arguments, "Cancel", cancel)}, artifacts)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runner.run("Check greeting"))
    report = json.loads((artifacts.path / "report.json").read_text())
    assert report["status"] == "interrupted"
    assert report["steps"] == 2
    assert report["plan"]["cases"][0]["id"] == "greeting"
    assert report["findings"][0]["status"] == "inconclusive"
    assert artifacts.observations == []


def test_runner_revalidates_custom_executor_output(tmp_path):
    class InvalidExecutor:
        async def decide(self, context):
            return Decision.model_construct(kind="action", action=None)

    runner, engine, artifacts = components(tmp_path, InvalidExecutor())
    with pytest.raises(ValueError, match="invalid action payload"):
        asyncio.run(runner.run("Check greeting"))
    assert not engine.calls
    assert json.loads((artifacts.path / "report.json").read_text())["status"] == "error"


def test_cli_import_and_planning_do_not_require_langgraph(tmp_path):
    script = '''
import importlib.abc
import sys
class NoGraph(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'langgraph', 'langchain', 'langchain_core'}:
            raise AssertionError('Unexpected graph dependency: ' + fullname)
sys.meta_path.insert(0, NoGraph())
from open_verify.cli import parser
from open_verify.runner import VerificationRunner
from open_verify.workflow import Verification
assert Verification is VerificationRunner
assert parser().parse_args(['check', '--plan-only']).plan_only
import asyncio
from pathlib import Path
from open_verify.artifacts import Artifacts
from open_verify.local_engine import LocalEngine
from open_verify.models import Decision
class Agent:
    async def decide(self, prompt):
        return Decision.model_validate({'kind': 'plan', 'plan': {
            'project_summary': 'fixture', 'startup': [], 'cases': [{
                'id': 'one', 'title': 'fixture', 'interface': 'terminal',
                'steps': ['read'], 'expected': 'hello'}]}})
root = Path(sys.argv[1])
artifacts = Artifacts(root / 'runs')
runner = VerificationRunner(Agent(), LocalEngine(root, artifacts), artifacts,
                            plan_only=True, progress=lambda _: None)
assert asyncio.run(runner.run('check'))['status'] == 'planned'
'''
    source = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)],
                            env={**os.environ, "PYTHONPATH": source}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_core_runner_imports_no_provider_or_engine_implementation():
    script = '''
import importlib.abc
import sys
class CoreOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname in {'open_verify.local_engine', 'open_verify.auth', 'open_verify.agent'} or fullname.split('.')[0] in {'playwright', 'langgraph_acp', 'langgraph'}:
            raise AssertionError('Core imported an implementation: ' + fullname)
sys.meta_path.insert(0, CoreOnly())
from open_verify.runner import VerificationRunner
assert VerificationRunner
'''
    source = str(Path(__file__).resolve().parents[1] / "src")
    result = subprocess.run([sys.executable, "-c", script],
                            env={**os.environ, "PYTHONPATH": source}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
