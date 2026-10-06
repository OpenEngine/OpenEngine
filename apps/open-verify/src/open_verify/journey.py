"""Deterministic act/assert scheduling over checked engines and replaceable reasoning."""

import asyncio
import hashlib
import json
from copy import deepcopy
from urllib.parse import urlsplit

from open_verify.journey_spec import (
    ActDecision,
    ActStep,
    AssertStep,
    Judgment,
    StepAction,
    StepResult,
)
from open_verify.media import encode_gif
from open_verify.replay_cache import (
    REPLAY_TOOLS,
    RecordedAction,
    RecordedStep,
    ReplaySession,
    ReplayStopped,
    screen_hash,
)
from open_verify.step_executor import JourneyExecutor
from open_verify.test_export import save_browser_test
from open_verify.test_spec import (
    ExpectSameURL,
    ExpectURL,
    RememberURL,
    BrowserTest,
    Click,
    Fill,
    Locator,
    NavigateURL,
    Press,
    Reload,
    ReplayBarrier,
    TestResult,
)
from open_verify.visual import VisualUnavailable, load_visual

ACT_TOOLS = frozenset({"browser_open", "browser_snapshot", "browser_click", "browser_fill",
                       "browser_press", "browser_reload", "browser_click_node", "browser_fill_node", "browser_press_node"})


class StepStopped(Exception):
    """A runtime-assigned limit or unavailable prerequisite, never a model-picked code."""

    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code


class StepBudget:
    """Count real provider requests, including repair, and requested tool actions."""

    def __init__(self, step):
        self.step = step
        self.model_calls = 0
        self.actions = 0

    def model_call(self):
        if self.model_calls >= self.step.max_model_calls:
            raise StepStopped("STEP_MODEL_LIMIT", "Step exhausted its model-call budget")
        self.model_calls += 1

    def action(self):
        if self.actions >= self.step.max_actions:
            raise StepStopped("STEP_ACTION_LIMIT", "Step exhausted its action budget")
        self.actions += 1


class JourneyRunner:
    """Run the plan's immutable goals and checks, then export exactly the observed trace."""

    def __init__(self, engine, artifacts, executor: JourneyExecutor | None, *, authentication=None,
                 progress=print, replay_cache=None, cache_mode="auto"):
        self.engine = engine
        self.artifacts = artifacts
        self.executor = executor
        self.authentication = authentication
        self.progress = progress
        self.attempt = 0
        self.replay_cache, self.cache_mode = replay_cache, cache_mode

    async def check_readiness(self, case):
        """Check setup through a fresh app session without executing journey actions."""
        results, engine = [], None
        status, detail = "passed", "Setup readiness confirmed"
        cache = ReplaySession(None, "off", case, self.engine, self.artifacts, self.progress)
        try:
            async with asyncio.timeout(30):
                engine = await self.engine.open_journey(url=case.journey.url,
                    authenticated=case.journey.authenticated, authentication=self.authentication)
                await self.observe(engine, "browser_open", {"url": case.journey.url})
            for index, check in enumerate(case.journey.readiness):
                self.progress(f"  Setup readiness: {check.instruction}")
                result = await self.run_step(engine, check, index, [], [], {},
                                             case.journey.url, cache)
                results.append(result.model_dump())
                if result.status != "passed":
                    status, detail = "blocked", result.detail
                    break
        except Exception as exc:
            status, detail = "blocked", f"{type(exc).__name__}: {exc}"
        finally:
            if engine is not None:
                try:
                    async with asyncio.timeout(15):
                        errors = await engine.close()
                    if errors:
                        status, detail = "blocked", "Readiness cleanup failed: " + "; ".join(errors)
                except Exception as exc:
                    status, detail = "blocked", f"Readiness cleanup failed: {exc}"
        return {"case_id": case.id, "status": status, "detail": detail, "checks": results}

    async def run(self, case, *, capture_media=False, on_result=None):
        """Own the case context, preserve partial evidence and never rerun to obtain media."""
        self.attempt += 1
        journey = case.journey.model_copy(deep=True)
        results, trace, checks, screenshots, omissions = [], [], {}, [], []
        baselines = {}
        engine = None
        cache = ReplaySession(self.replay_cache, self.cache_mode, case, self.engine, self.artifacts, self.progress)
        interrupted = None
        status, detail = "blocked", "Journey did not start"
        first_evidence = len(self.artifacts.observations)
        identity = hashlib.sha256(case.id.encode()).hexdigest()[:16]
        try:
            cache.prepare()
            async with asyncio.timeout(30):
                engine = await self.engine.open_journey(url=journey.url,
                    authenticated=journey.authenticated, authentication=self.authentication)
                await self.observe(engine, "browser_open", {"url": journey.url})
            for index, step in enumerate(journey.steps):
                self.progress(f"  {step.kind} {index + 1}/{len(journey.steps)}: {step.instruction}")
                result = await self.run_step(engine, step, index, results, trace, checks, journey.url, cache, baselines)
                results.append(result)
                self.artifacts.record("journey_step", {"case_id": case.id}, result.model_dump(),
                                      result.status == "passed")
                status, detail = result.status, result.detail
                if status != "passed":
                    break
        except asyncio.CancelledError as exc:
            interrupted = exc
            if getattr(exc, "step_result", None) is not None:
                results.append(exc.step_result)
                self.artifacts.record("journey_step", {"case_id": case.id}, exc.step_result.model_dump(), False)
            status, detail = "blocked", "Journey interrupted"
        except Exception as exc:
            status, detail = "blocked", f"{type(exc).__name__}: {exc}"
        finally:
            if engine is not None:
                try:
                    async with asyncio.timeout(15):
                        errors = await engine.close()
                    if errors:
                        status, detail = "blocked", "Cleanup failed: " + "; ".join(errors)
                except asyncio.CancelledError as exc:
                    interrupted = interrupted if interrupted is not None else exc
                    status, detail = "blocked", "Journey interrupted during cleanup"
                except Exception as exc:
                    status, detail = "blocked", f"Cleanup failed: {type(exc).__name__}: {exc}"
            cache.commit(status)
            for index in range(len(results), len(journey.steps)):
                step = journey.steps[index]
                results.append(StepResult(index=index, kind=step.kind, instruction=step.instruction,
                    status="blocked", code="NOT_RUN", detail="Not run after the preceding failure or interruption"))
            for step in journey.steps:
                if isinstance(step, AssertStep) and step.instruction not in checks:
                    checks[step.instruction] = [len(trace)]
                    trace.append(ReplayBarrier(kind="requires_verification",
                        reason=f"Unexecuted check requires verification: {step.instruction}"))
            if status != "passed":
                trace.append(ReplayBarrier(kind="requires_verification", reason=detail))
            if len(trace) > 40:
                omissions.append("Replay exceeded 40 operations; complete actions remain in evidence.jsonl.")
                trace = [ReplayBarrier(kind="requires_verification", reason=omissions[-1])]
                checks = {name: [0] for name in checks}
            test = BrowserTest(case_id=case.id, url=journey.url, authenticated=journey.authenticated,
                steps=trace, checks=checks, timeout=min(120, sum(s.timeout for s in journey.steps)))
            login = getattr(self.authentication, "request", None)
            path, _ = save_browser_test(test, self.artifacts, attempt=self.attempt,
                                       login=login.model_dump() if login and journey.authenticated else None)
            relative = path.relative_to(self.artifacts.path).as_posix()
            rerun = ["python", relative]
            if login and journey.authenticated:
                rerun.append("--login")
            for origin in self.engine.environment().get("allowed_origins", []):
                rerun.extend(["--allow-origin", origin])
            if journey.authenticated and self.authentication is not None:
                if getattr(self.authentication, "summary", ""):
                    detail = self.authentication.summary + " " + detail
                if capture_media:
                    screenshots.extend(p for p in getattr(self.authentication, "screenshots", []) if p.endswith(".png"))
                    omissions.extend(getattr(self.authentication, "omissions", []))
            for evidence in self.artifacts.observations[first_evidence:]:
                screenshot = evidence.get("result", {}).get("screenshot")
                if screenshot and screenshot not in screenshots:
                    screenshots.append(screenshot)
            if any(isinstance(s, AssertStep) and s.check is None for s in journey.steps):
                omissions.append("Model assertions need a live judge; exported replay stops at explicit verification barriers.")
            if any(isinstance(s, ReplayBarrier) for s in trace) and not omissions:
                omissions.append("Exported replay contains an explicit verification barrier; inspect its reason before replay.")
            result = TestResult(case_id=case.id, runner="playwright", status=status, detail=detail,
                test_file=relative, rerun=rerun, screenshots=screenshots, omissions=omissions,
                checkpoints=[{key: s.model_dump()[key] for key in ('instruction', 'status', 'detail', 'code')}
                             for s in results if s.kind == 'assert'])
            folder = self.artifacts.path / "journeys"
            folder.mkdir(exist_ok=True)
            self.artifacts.write(f"journeys/{identity}.json", {
                "case_id": case.id, "status": status, "detail": detail,
                "steps": [s.model_dump() for s in results], "test": relative,
                "cache": {"mode": cache.mode, "key": cache.key, "events": cache.events},
            })
            if on_result:
                on_result(result)
        if interrupted is not None:
            raise interrupted
        if capture_media and screenshots:
            pending = "GIF omitted: encoding has not completed."
            result.omissions.append(pending)
            if on_result:
                on_result(result)
            destination = self.artifacts.path / f"journey-{identity}.gif"
            try:
                reason = await encode_gif([self.artifacts.path / p for p in screenshots], destination)
                if reason:
                    result.omissions.append(reason)
                else:
                    result.screenshots.append(destination.name)
            except asyncio.CancelledError:
                result.omissions.remove(pending)
                result.omissions.append("GIF omitted: encoding was interrupted.")
                if on_result:
                    on_result(result)
                raise
            except Exception as exc:
                result.omissions.append(f"GIF omitted: {exc}")
            result.omissions.remove(pending)
            if on_result:
                on_result(result)
        return result

    async def observe(self, engine, tool="browser_snapshot", arguments=None):
        """Use the engine's fresh observation, never an actor-written summary."""
        receipt = await engine.execute(tool, arguments or {})
        if not receipt["ok"]:
            raise StepStopped("OBSERVATION_UNAVAILABLE", str(receipt["result"]))
        return receipt

    async def run_step(self, engine, step, index, completed, trace, checks, entry_url, cache, baselines=None):
        """Enforce one clock across observation, session setup, model repair and execution."""
        budget = StepBudget(step)
        first = len(self.artifacts.observations)
        code = None
        if baselines is None:
            baselines = {}
        try:
            async with asyncio.timeout(step.timeout):
                if isinstance(step, ActStep):
                    status, detail = await self.act(engine, step, budget, completed, trace, entry_url, cache, index)
                else:
                    checks.setdefault(step.instruction, []).append(len(trace))
                    trace.append(step.check or ReplayBarrier(kind="requires_verification",
                        reason=f"Live {step.mode} judgment required: {step.instruction}"))
                    if step.check is not None:
                        check = step.check
                        if isinstance(check, ExpectSameURL):
                            if check.baseline not in baselines:
                                raise StepStopped('BASELINE_UNAVAILABLE', 'URL baseline is unavailable')
                            check = ExpectURL(kind='expect_url', url=baselines[check.baseline]['url'])
                        receipt = await engine.assert_check(check)
                        status, detail = receipt["result"]["status"], receipt["result"]["detail"]
                        await self.observe(engine)
                    else:
                        if self.executor is None:
                            raise StepStopped("MODEL_UNAVAILABLE", "Model assertion requires a journey executor")
                        if step.mode == "visual":
                            judge = getattr(self.executor, "judge_visual", None)
                            if not callable(judge):
                                raise VisualUnavailable("VISUAL_INPUT_UNSUPPORTED", "The journey executor does not support visual judgments")
                            if "browser_visual_snapshot" not in engine.catalog("execute"):
                                raise VisualUnavailable("VISUAL_EVIDENCE_UNAVAILABLE", "The engine does not offer visual observations")
                            receipt = await engine.execute("browser_visual_snapshot", {})
                            if not receipt["ok"]:
                                raise VisualUnavailable("VISUAL_EVIDENCE_UNAVAILABLE", "The engine could not capture visual evidence")
                            fresh = receipt["result"]
                            image = load_visual(self.artifacts.path, fresh)
                            evidence = {"url": fresh["url"], "image": image.metadata()}
                            response = await judge(step.instruction, deepcopy(evidence), image, on_call=budget.model_call)
                        else:
                            fresh = (await self.observe(engine))["result"]
                            evidence = {key: fresh[key] for key in ("url", "snapshot", "truncated") if key in fresh}
                            if step.compare_to:
                                if step.compare_to not in baselines:
                                    raise StepStopped('BASELINE_UNAVAILABLE', 'Comparison baseline is unavailable')
                                evidence['baseline'] = deepcopy(baselines[step.compare_to])
                            response = await self.executor.judge(step.instruction, deepcopy(evidence), on_call=budget.model_call)
                        judgment = Judgment.model_validate(response.model_dump())
                        status = {"holds": "passed", "fails": "failed", "inconclusive": "blocked"}[judgment.verdict]
                        detail = judgment.explanation
                        if judgment.verdict == "inconclusive":
                            code = "ASSERTION_INCONCLUSIVE"
                    if status == 'passed' and step.remember_as:
                        observed = await self.observe(engine)
                        fresh = observed['result']
                        baselines[step.remember_as] = {key: fresh[key] for key in ('url', 'snapshot', 'truncated') if key in fresh}
                        baselines[step.remember_as]['evidence_id'] = observed['id']
                        trace.append(RememberURL(kind='remember_url', name=step.remember_as,
                                                 wait_for_navigation=fresh.get('url') != entry_url))
        except asyncio.CancelledError as exc:
            exc.step_result = StepResult(index=index, kind=step.kind, instruction=step.instruction,
                status="blocked", code="STEP_INTERRUPTED", detail="Step interrupted",
                model_calls=budget.model_calls, actions=budget.actions,
                evidence=[e["id"] for e in self.artifacts.observations[first:]], **cache.step_info.get(index, {}))
            raise
        except TimeoutError:
            status, detail, code = "blocked", "Step deadline exceeded", "STEP_TIMEOUT"
        except (StepStopped, ReplayStopped, VisualUnavailable) as exc:
            status, detail, code = "blocked", str(exc), exc.code
        except Exception as exc:
            status, detail, code = "blocked", f"{type(exc).__name__}: {exc}", "STEP_ERROR"
        return StepResult(index=index, kind=step.kind, instruction=step.instruction, status=status,
            detail=detail, code=code, model_calls=budget.model_calls, actions=budget.actions,
            evidence=[e["id"] for e in self.artifacts.observations[first:]], **cache.step_info.get(index, {}))

    async def act(self, engine, step, budget, completed, trace, entry_url, cache, index):
        """Only the engine's offered browser tools can execute inside an action step."""
        observation = await self.observe(engine)
        cached = cache.lookup(index, observation["result"])
        if cached is not None:
            replayed = await self.replay(engine, cached, budget, trace, entry_url, cache)
            if replayed:
                cache.pending[index] = cached
                return "passed", "Recorded actions completed; fresh assertions follow"
            observation = await self.observe(engine)
        if self.executor is None:
            raise StepStopped("MODEL_UNAVAILABLE", "Action step requires a journey executor")
        await self.executor.begin()
        initial = screen_hash(observation["result"])
        recorded = []
        tools = {name: spec for name, spec in engine.catalog("execute").items() if name in ACT_TOOLS}
        last_signature, repetitions, failures, feedback = None, 0, 0, ""
        while True:
            if budget.model_calls >= step.max_model_calls:
                raise StepStopped("STEP_MODEL_LIMIT", "Step exhausted its model-call budget")
            response = await self.executor.act(deepcopy({
                "goal": step.instruction, "observation": observation,
                "completed_steps": [s.model_dump() for s in completed[-5:]],
                "tools": tools, "feedback": feedback,
                "remaining_actions": step.max_actions - budget.actions,
                "remaining_model_calls": step.max_model_calls - budget.model_calls,
            }), on_call=budget.model_call)
            decision = ActDecision.model_validate(response.model_dump())
            if decision.kind == "complete":
                if decision.outcome == "blocked":
                    return "blocked", "Action could not complete: " + decision.summary
                if cache.mode != "off" and cache.eligible:
                    final = screen_hash((await self.observe(engine))["result"])
                    if initial is not None and final is not None:
                        cache.pending[index] = RecordedStep(index=index, initial=initial, final=final, actions=recorded)
                    else:
                        cache.exclude(index, "Incomplete screen observation")
                # Completion advances to the fixed assertions; it is not a case verdict.
                return "passed", decision.summary
            budget.action()
            action = decision.action
            signature = json.dumps([action.tool, action.arguments], sort_keys=True)
            repetitions = repetitions + 1 if signature == last_signature else 1
            last_signature = signature
            if repetitions >= 5 or failures >= 5:
                raise StepStopped("STEP_LOOP_GUARD", "Repeated actions or failures stopped the step")
            if action.tool not in tools:
                cache.exclude(index, "Act requested a refused tool")
                observation = self.artifacts.record(action.tool, action.arguments,
                    {"error": "Tool is not available to this action step"}, False)
            else:
                before = None
                if cache.mode != "off" and cache.eligible:
                    before = screen_hash((await self.observe(engine))["result"])
                observation = await engine.execute(action.tool, action.arguments)
                durable = durable_action(action, observation)
                append_trace(trace, durable or action, entry_url, observation["ok"])
                if cache.mode != "off" and cache.eligible:
                    after = screen_hash(observation["result"])
                    if durable is None or durable.tool not in REPLAY_TOOLS or not observation["ok"] or before is None or after is None:
                        cache.exclude(index, "Act contains an ambiguous node, fill, failed action or incomplete observation")
                    else:
                        sanitized = durable.model_copy(update={"reason": "Recorded browser action"}, deep=True)
                        recorded.append(RecordedAction(action=sanitized, before=before, after=after))
            failures = 0 if observation["ok"] else failures + 1
            feedback = "Change approach or conclude; actions are repeating or failing." if repetitions >= 3 or failures >= 3 else ""


    async def replay(self, engine, recorded, budget, trace, entry_url, cache):
        """Check each screen around dispatch; never restart a partially replayed step."""
        started = False
        for item in recorded.actions:
            fresh = await self.observe(engine)
            if screen_hash(fresh["result"]) != item.before:
                cache.stale(recorded.index, "Screen changed before a recorded action", started=started)
                return False
            budget.action()
            started = True
            receipt = await engine.execute(item.action.tool, item.action.arguments)
            append_trace(trace, item.action, entry_url, receipt["ok"])
            if not receipt["ok"] or screen_hash(receipt["result"]) != item.after:
                cache.stale(recorded.index, "Recorded action failed or its resulting screen changed", started=True)
        final = await self.observe(engine)
        if screen_hash(final["result"]) != recorded.final:
            cache.stale(recorded.index, "Final act screen differs from the recording", started=started)
            return False
        return True



def durable_action(action, receipt):
    """Only the engine may translate transient node references into replayable actions."""
    if action.tool not in {"browser_click_node", "browser_fill_node", "browser_press_node"}:
        return action
    resolved = receipt.get("result", {}).get("resolved_action")
    if not receipt["ok"] or resolved is None:
        return None
    parsed = StepAction.model_validate(resolved)
    if parsed.tool != action.tool.removesuffix("_node"):
        return None
    return parsed


def append_trace(trace, action, entry_url, succeeded):
    """Live and replayed operations use the same honest export path."""
    if action.tool == "browser_snapshot":
        return
    try:
        recorded = recorded_action(action, entry_url, succeeded)
    except (ValueError, KeyError) as exc:
        recorded = ReplayBarrier(kind="requires_verification", reason=f"Action cannot be exported: {exc}")
    trace.append(recorded)


def recorded_action(action, entry_url, succeeded):
    """Translate executed actions only; unsupported or uncertain replay fails closed."""
    args = action.arguments
    if not succeeded:
        return ReplayBarrier(kind="requires_verification", reason=f"Action {action.tool} failed; verify its effect before replay")
    if action.tool in {"browser_click", "browser_fill", "browser_press"}:
        locator = Locator.model_validate({k: args[k] for k in ("by", "role", "name") if k in args})
        if action.tool == "browser_click":
            return Click(kind="click", locator=locator)
        if action.tool == "browser_fill":
            return Fill(kind="fill", locator=locator, value=args["value"])
        return Press(kind="press", locator=locator, key=args["key"])
    if action.tool == "browser_reload":
        return Reload(kind="reload")
    if action.tool == "browser_open":
        url, start = urlsplit(args["url"]), urlsplit(entry_url)
        if (url.scheme, url.netloc) == (start.scheme, start.netloc):
            return NavigateURL(kind="navigate_url", url=args["url"])
    return ReplayBarrier(kind="requires_verification", reason=f"Action {action.tool} requires live verification")
