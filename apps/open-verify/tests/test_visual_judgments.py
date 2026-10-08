"""Fresh pixels, independent judgments and explicit failures across the visual path."""

import asyncio
import base64
import json
import struct
import sys
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from langgraph_acp import ACPEventType
from test_journeys import Actor, StepBudget, StepStopped, case, read_steps
from test_replay_cache import NoActor
from test_tools import web_app as web_app

from open_verify.agent import ACPDecisionAgent, provider_for
from open_verify.artifacts import Artifacts
from open_verify.journey import JourneyRunner
from open_verify.journey_spec import AssertStep, Judgment
from open_verify.replay_cache import ReplayCache
from open_verify.step_executor import AgentJourneyExecutor
from open_verify.tools import LocalTools
from open_verify.visual import (
    MAX_IMAGE_BYTES,
    PNG_SIGNATURE,
    VisualImage,
    VisualUnavailable,
    load_visual,
)


def png(*, width=1, height=1, compressed=None):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    return (PNG_SIGNATURE + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', compressed if compressed is not None else zlib.compress(b'\0\xff\0\0'))
            + chunk(b'IEND', b''))


def visual_case(url, **options):
    target = case(url, semantic=True)
    target.journey.steps[-1] = AssertStep(kind='assert', instruction='The cart is visually readable', mode='visual', **options)
    target.checks = ['The cart is visually readable']
    return target


class VisualActor(Actor):
    async def judge_visual(self, instruction, observation, image, *, on_call):
        on_call()
        self.judgments.append((instruction, observation, image))
        return Judgment(explanation='Evaluated the current viewport', verdict=self.verdict)


class VisualNoActor(NoActor, VisualActor):
    pass


def test_visual_schema_is_explicit_and_exact_checks_cannot_be_combined():
    assert AssertStep(kind='assert', instruction='Text').mode == 'semantic'
    assert AssertStep(kind='assert', instruction='Pixels', mode='visual').mode == 'visual'
    with pytest.raises(ValueError, match='exact check'):
        AssertStep(kind='assert', instruction='Pixels', mode='visual', check={'kind': 'expect_text', 'text': 'Visible'})
    with pytest.raises(ValueError):
        AssertStep(kind='assert', instruction='Pixels', mode='guess')


@pytest.mark.parametrize(('verdict', 'status', 'code'), [
    ('holds', 'passed', None), ('fails', 'failed', None), ('inconclusive', 'blocked', 'ASSERTION_INCONCLUSIVE'),
])
def test_visual_judge_uses_exact_fresh_viewport_and_export_barrier(tmp_path, web_app, verdict, status, code):
    artifacts = Artifacts(tmp_path / 'runs')
    actor = VisualActor(verdict=verdict)
    result = asyncio.run(JourneyRunner(LocalTools(tmp_path, artifacts, headless=True), artifacts,
        actor, progress=lambda _: None).run(visual_case(web_app)))
    assert result.status == status, result.detail
    step = read_steps(artifacts)[-1]
    assert step['code'] == code and step['model_calls'] == 1
    instruction, evidence, image = actor.judgments[0]
    assert set(evidence) == {'url', 'image'}
    assert 'snapshot' not in evidence and 'screen_diff' not in evidence
    assert (image.width, image.height) == (1280, 720)
    capture = next(e for e in artifacts.observations if e['tool'] == 'browser_visual_snapshot')
    assert capture['id'] in step['evidence']
    assert capture['result']['image'] == image.metadata()
    assert (artifacts.path / capture['result']['screenshot']).read_bytes() == image.data
    assert capture['result']['screenshot'] in result.screenshots
    click = next(e for e in artifacts.observations if e['tool'] == 'browser_click')
    assert artifacts.observations.index(click) < artifacts.observations.index(capture)
    source = (artifacts.path / result.test_file).read_text()
    assert 'Live visual judgment required' in source and 'raise RuntimeError' in source
    assert base64.b64encode(image.data).decode() not in (artifacts.path / 'evidence.jsonl').read_text()


def test_visual_capture_is_viewport_only_and_changes_with_pixels(tmp_path):
    async def run():
        engine = LocalTools(tmp_path, Artifacts(tmp_path / 'runs'), headless=True)
        try:
            page = await engine.browser_page()
            await page.set_content('<body style="margin:0;height:3000px;background:blue"><canvas></canvas></body>')
            first = await engine.execute('browser_visual_snapshot', {})
            await page.evaluate("document.body.style.background='red'")
            second = await engine.execute('browser_visual_snapshot', {})
            assert first['ok'] and second['ok']
            assert first['result']['image']['height'] == 720
            assert first['result']['image']['sha256'] != second['result']['image']['sha256']
            assert first['result']['screenshot'] != second['result']['screenshot']
            assert not (await engine.execute('browser_visual_snapshot', {}, stage='discover'))['ok']
        finally:
            await engine.close()
    asyncio.run(run())


def test_strict_replay_uses_fresh_visual_judge_and_invalidates_on_failure(tmp_path, web_app):
    cache = ReplayCache(tmp_path / 'cache', tmp_path)
    async def run(actor, mode):
        artifacts = Artifacts(tmp_path / 'runs')
        result = await JourneyRunner(LocalTools(tmp_path, artifacts, headless=True), artifacts,
            actor, replay_cache=cache, cache_mode=mode, progress=lambda _: None).run(visual_case(web_app))
        return result, artifacts
    first, _ = asyncio.run(run(VisualActor(), 'auto'))
    assert first.status == 'passed'
    raw = next(cache.directory.glob('*.json')).read_text()
    assert 'image/png' not in raw and 'sha256' not in raw and 'viewport' not in raw
    judge = VisualNoActor()
    second, artifacts = asyncio.run(run(judge, 'strict'))
    assert second.status == 'passed'
    assert read_steps(artifacts)[0]['model_calls'] == 1 and read_steps(artifacts)[0]['cache'] == 'hit'
    assert len(judge.judgments) == 1
    failed, artifacts = asyncio.run(run(VisualNoActor(verdict='fails'), 'auto'))
    assert failed.status == 'failed'
    assert read_steps(artifacts)[0]['cache'] == 'hit'
    assert not list(cache.directory.glob('*.json'))


def test_old_executor_cannot_silently_answer_visual_assertions(tmp_path, web_app):
    artifacts = Artifacts(tmp_path / 'runs')
    actor = Actor()
    result = asyncio.run(JourneyRunner(LocalTools(tmp_path, artifacts, headless=True), artifacts,
        actor, progress=lambda _: None).run(visual_case(web_app)))
    assert result.status == 'blocked'
    assert read_steps(artifacts)[-1]['code'] == 'VISUAL_INPUT_UNSUPPORTED'
    assert not actor.judgments


@pytest.mark.parametrize('fault', ['capture', 'tamper', 'missing'])
def test_bad_capture_cannot_reach_judge(tmp_path, web_app, monkeypatch, fault):
    original = LocalTools.browser_visual_snapshot
    async def broken(engine, args):
        if fault == 'capture':
            raise RuntimeError('Capture unavailable')
        result = await original(engine, args)
        path = engine.artifacts.path / result['screenshot']
        if fault == 'tamper':
            path.write_bytes(png())
        else:
            path.unlink()
        return result
    monkeypatch.setattr(LocalTools, 'browser_visual_snapshot', broken)
    artifacts = Artifacts(tmp_path / 'runs')
    actor = VisualActor()
    result = asyncio.run(JourneyRunner(LocalTools(tmp_path, artifacts, headless=True), artifacts,
        actor, progress=lambda _: None).run(visual_case(web_app)))
    assert result.status == 'blocked' and not actor.judgments
    assert read_steps(artifacts)[-1]['code'] == ('VISUAL_EVIDENCE_UNAVAILABLE' if fault == 'capture' else 'VISUAL_EVIDENCE_INVALID')


@pytest.mark.parametrize('data', [b'not png', png()[:-1], png() + b'x', png(width=0), png(width=4097),
                                 png(width=4096, height=4096), png(compressed=b'broken'),
                                 png(compressed=zlib.compress(b'x' * 1000)), b'x' * (MAX_IMAGE_BYTES + 1)])
def test_invalid_png_is_rejected_before_transport(data):
    with pytest.raises(VisualUnavailable):
        VisualImage(data)


@pytest.mark.parametrize('name', ['../outside.png', '/outside.png', 'a/b.png', 'image.jpg', 'link.png', 'missing.png', 'pipe.png'])
def test_image_files_are_bounded_local_regular_files(tmp_path, name):
    import os
    good = VisualImage(png())
    (tmp_path / 'good.png').write_bytes(good.data)
    (tmp_path / 'link.png').symlink_to(tmp_path / 'good.png')
    os.mkfifo(tmp_path / 'pipe.png')
    with pytest.raises(VisualUnavailable):
        load_visual(tmp_path, {'screenshot': name, 'image': good.metadata()})
    assert load_visual(tmp_path, {'screenshot': 'good.png', 'image': good.metadata()}) == good


def test_visual_executor_starts_fresh_and_keeps_pixels_out_of_text_prompt():
    class Transport:
        resets = 0
        async def reset_session(self):
            self.resets += 1
        async def respond(self, prompt, schema, *, on_call, image=None):
            on_call()
            self.prompt, self.image = prompt, image
            return Judgment(explanation='Pixels', verdict='holds')
    transport = Transport()
    executor = AgentJourneyExecutor(transport)
    image = VisualImage(png())
    result = asyncio.run(executor.judge_visual('Red pixel', {'url': 'http://localhost', 'image': image.metadata()}, image, on_call=lambda: None))
    assert result.verdict == 'holds' and transport.resets == 1
    assert transport.image is image
    assert base64.b64encode(image.data).decode() not in transport.prompt
    assert 'untrusted' in transport.prompt and 'outside this viewport' in transport.prompt


class Session:
    def __init__(self, turns):
        self.turns = iter(turns)
        self.prompts = []
        self.cancelled = self.closed = False

    async def prompt(self, prompt):
        self.prompts.append(prompt)
        turn = next(self.turns)
        if turn == 'native':
            yield SimpleNamespace(type=ACPEventType.TOOL_STARTED, data={})
        else:
            yield SimpleNamespace(type=ACPEventType.MESSAGE_DELTA, data={'content': {'type': 'text', 'text': turn}})
        yield SimpleNamespace(type=ACPEventType.PROMPT_COMPLETED, data={'stopReason': 'end_turn'})

    async def cancel(self):
        self.cancelled = True

    async def close(self):
        self.closed = True


GOOD = json.dumps({'explanation': 'Pixels show red', 'verdict': 'holds'})


@pytest.mark.parametrize('first', ['{}', 'native'])
def test_acp_repair_and_native_recovery_reattach_identical_image(tmp_path, first):
    initial, replacement = Session([first, GOOD]), Session([GOOD])
    class Client:
        capabilities = SimpleNamespace(prompt_image=True)
        async def new_session(self, **kwargs):
            return replacement
    agent = ACPDecisionAgent(None, tmp_path)
    agent.client, agent.session = Client(), initial
    budget = StepBudget(AssertStep(kind='assert', instruction='Red', mode='visual'))
    image = VisualImage(png())
    result = asyncio.run(agent.respond('Red pixel required', Judgment, image=image, on_call=budget.model_call))
    assert result.verdict == 'holds' and budget.model_calls == 2
    prompts = initial.prompts + replacement.prompts
    assert len(prompts) == 2
    assert all(p[1] == image.content_block() for p in prompts)
    assert all('Red pixel required' in p[0]['text'] for p in prompts)
    if first == 'native':
        assert initial.cancelled and initial.closed


def test_image_capability_is_checked_before_prompt_or_model_budget(tmp_path):
    agent = ACPDecisionAgent(None, tmp_path)
    agent.client = SimpleNamespace(capabilities=SimpleNamespace(prompt_image=False))
    agent.session = Session([GOOD])
    budget = StepBudget(AssertStep(kind='assert', instruction='Red', mode='visual'))
    with pytest.raises(VisualUnavailable, match='does not advertise'):
        asyncio.run(agent.respond('Red', Judgment, image=VisualImage(png()), on_call=budget.model_call))
    assert budget.model_calls == 0 and agent.session.prompts == []


def test_image_repair_cannot_exceed_step_model_budget(tmp_path):
    agent = ACPDecisionAgent(None, tmp_path)
    agent.client = SimpleNamespace(capabilities=SimpleNamespace(prompt_image=True))
    agent.session = Session(['{}', GOOD])
    budget = StepBudget(AssertStep(kind='assert', instruction='Red', mode='visual', max_model_calls=1))
    with pytest.raises(StepStopped):
        asyncio.run(agent.respond('Red', Judgment, image=VisualImage(png()), on_call=budget.model_call))
    assert budget.model_calls == 1 and len(agent.session.prompts) == 1


def test_real_acp_wire_carries_image_content_not_a_path(tmp_path):
    log = tmp_path / 'wire.json'
    agent = ACPDecisionAgent(provider_for('fixture', [sys.executable, str(Path(__file__).with_name('fake_visual_agent.py')), str(log)]), tmp_path)
    image = VisualImage(png())
    async def run():
        try:
            return await agent.respond('Red', Judgment, image=image)
        finally:
            await agent.close()
    assert asyncio.run(run()).verdict == 'holds'
    blocks = json.loads(log.read_text())
    assert blocks[1] == image.content_block()
    assert 'file://' not in json.dumps(blocks)



def test_visual_judge_cancellation_preserves_checkpoint_and_screenshot(tmp_path, web_app):
    async def run():
        entered = asyncio.Event()
        class SlowJudge(VisualActor):
            async def judge_visual(self, instruction, observation, image, *, on_call):
                on_call()
                entered.set()
                await asyncio.Event().wait()
        artifacts = Artifacts(tmp_path / 'runs')
        task = asyncio.create_task(JourneyRunner(LocalTools(tmp_path, artifacts, headless=True), artifacts,
            SlowJudge(), progress=lambda _: None).run(visual_case(web_app)))
        await asyncio.wait_for(entered.wait(), 15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        step = read_steps(artifacts)[-1]
        assert step['code'] == 'STEP_INTERRUPTED' and step['model_calls'] == 1
        capture = next(e for e in artifacts.observations if e['tool'] == 'browser_visual_snapshot')
        assert capture['id'] in step['evidence']
        assert (artifacts.path / capture['result']['screenshot']).exists()
    asyncio.run(run())


def test_visual_transport_timeout_is_bounded_and_keeps_image_evidence(tmp_path, web_app):
    class SlowJudge(VisualActor):
        async def judge_visual(self, instruction, observation, image, *, on_call):
            on_call()
            await asyncio.sleep(10)
    artifacts = Artifacts(tmp_path / 'runs')
    result = asyncio.run(JourneyRunner(LocalTools(tmp_path, artifacts, headless=True), artifacts,
        SlowJudge(), progress=lambda _: None).run(visual_case(web_app, timeout=1)))
    assert result.status == 'blocked' and result.screenshots
    assert read_steps(artifacts)[-1]['code'] == 'STEP_TIMEOUT'


def test_engine_without_visual_catalog_cannot_substitute_text(tmp_path, web_app, monkeypatch):
    original = LocalTools.catalog
    monkeypatch.setattr(LocalTools, 'catalog', lambda engine, stage: {
        k: v for k, v in original(engine, stage).items() if k != 'browser_visual_snapshot'})
    artifacts = Artifacts(tmp_path / 'runs')
    actor = VisualActor()
    result = asyncio.run(JourneyRunner(LocalTools(tmp_path, artifacts, headless=True), artifacts,
        actor, progress=lambda _: None).run(visual_case(web_app)))
    assert result.status == 'blocked' and not actor.judgments
    assert read_steps(artifacts)[-1]['code'] == 'VISUAL_EVIDENCE_UNAVAILABLE'


def test_unsupported_acp_input_blocks_journey_without_model_call(tmp_path, web_app):
    agent = ACPDecisionAgent(None, tmp_path)
    agent.client = SimpleNamespace(capabilities=SimpleNamespace(prompt_image=False))
    executor = AgentJourneyExecutor(agent)
    target = visual_case(web_app)
    target.journey.steps = target.journey.steps[-1:]
    artifacts = Artifacts(tmp_path / 'runs')
    result = asyncio.run(JourneyRunner(LocalTools(tmp_path, artifacts, headless=True), artifacts,
        executor, progress=lambda _: None).run(target))
    assert result.status == 'blocked'
    assert read_steps(artifacts)[-1]['code'] == 'VISUAL_INPUT_UNSUPPORTED'
    assert read_steps(artifacts)[-1]['model_calls'] == 0


def test_oversized_file_and_checksum_corruption_are_refused(tmp_path):
    image = VisualImage(png())
    path = tmp_path / 'image.png'
    with path.open('wb') as stream:
        stream.truncate(MAX_IMAGE_BYTES + 1)
    with pytest.raises(VisualUnavailable):
        load_visual(tmp_path, {'screenshot': path.name, 'image': image.metadata()})
    broken = bytearray(png())
    broken[-1] ^= 1
    with pytest.raises(VisualUnavailable):
        VisualImage(bytes(broken))
