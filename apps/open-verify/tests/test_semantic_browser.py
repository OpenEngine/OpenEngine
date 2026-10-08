"""Real-browser reference identity, stale refusal, diffs and durable replay boundaries."""

import asyncio
import json

import pytest
from test_journeys import Actor, case, complete, read_steps
from test_replay_cache import NoActor
from test_tools import web_app as web_app

from open_verify.artifacts import Artifacts
from open_verify.journey import JourneyRunner
from open_verify.journey_spec import ActDecision
from open_verify.replay_cache import ReplayCache
from open_verify.tools import LocalTools


async def snapshot(engine):
    receipt = await engine.execute('browser_snapshot', {})
    assert receipt['ok'], receipt
    assert 'semantic_error' not in receipt['result'], receipt
    return receipt['result']


def run_html(tmp_path, html, check):
    async def run():
        artifacts = Artifacts(tmp_path / 'runs')
        engine = LocalTools(tmp_path, artifacts, headless=True)
        try:
            page = await engine.browser_page()
            await page.set_content(html)
            await check(engine, page, await snapshot(engine))
        finally:
            assert await engine.close() == []
    asyncio.run(run())


def reference(screen, name, index=0):
    node = [n for n in screen['nodes'] if n['name'] == name][index]
    return {'observation_id': screen['observation_id'], 'node_id': node['id']}


def test_duplicate_names_bind_the_exact_element_without_guessing_a_locator(tmp_path):
    html = '''<button onclick="document.querySelector('p').textContent='First'">Delete</button>
    <button onclick="document.querySelector('p').textContent='Second'">Delete</button><p>Ready</p>'''
    async def check(engine, page, first):
        assert len(first['nodes']) == 2
        result = await engine.execute('browser_click_node', reference(first, 'Delete', 1))
        assert result['ok'], result
        assert await page.locator('p').inner_text() == 'Second'
        assert 'resolved_action' not in result['result']
        assert result['result']['screen_diff']['counts']['text'] > 0
    run_html(tmp_path, html, check)


def test_unchanged_observation_retains_ids_and_an_empty_diff(tmp_path):
    async def check(engine, page, first):
        second = await snapshot(engine)
        assert second['observation_id'] == first['observation_id']
        assert second['nodes'] == first['nodes']
        assert second['screen_diff']['unchanged']
        assert second['screen_diff']['from'] == first['observation_id']
        assert not second['screen_diff']['added']
    run_html(tmp_path, '<button>Save</button>', check)


@pytest.mark.parametrize('mutation', [
    "document.querySelector('p').textContent='Changed'",
    "document.querySelector('button').outerHTML='<button onclick=\"window.clicked=true\">Save</button>'",
    "document.querySelector('button').disabled=true",
    "document.querySelector('button').remove()",
])
def test_stale_reference_is_refused_before_side_effects(tmp_path, mutation):
    async def check(engine, page, first):
        await page.evaluate(mutation)
        result = await engine.execute('browser_click_node', reference(first, 'Save'))
        assert not result['ok'] and result['result']['code'] == 'STALE_OBSERVATION'
        assert not await page.evaluate('!!window.clicked')
    run_html(tmp_path, '<button onclick="window.clicked=true">Save</button><p>Ready</p>', check)


def test_reorder_preserves_physical_ids_but_requires_the_new_observation(tmp_path):
    html = '<div><button id="a">First</button><button id="b">Second</button></div>'
    async def check(engine, page, first):
        ids = {n['name']: n['id'] for n in first['nodes']}
        await page.evaluate("document.querySelector('div').prepend(document.querySelector('#b'))")
        second = await snapshot(engine)
        assert {n['name']: n['id'] for n in second['nodes']} == ids
        assert second['observation_id'] != first['observation_id']
        assert second['screen_diff']['changed']
        assert not second['screen_diff']['added'] and not second['screen_diff']['removed']
        result = await engine.execute('browser_click_node', reference(first, 'First'))
        assert result['result']['code'] == 'STALE_OBSERVATION'
    run_html(tmp_path, html, check)


def test_identical_replacement_has_new_id_and_added_removed_diff(tmp_path):
    async def check(engine, page, first):
        await page.evaluate("const old=document.querySelector('button'); old.replaceWith(old.cloneNode(true))")
        second = await snapshot(engine)
        assert second['nodes'][0]['id'] != first['nodes'][0]['id']
        assert second['screen_diff']['counts']['added'] == 1
        assert second['screen_diff']['counts']['removed'] == 1
        assert second['semantic_fingerprint'] == first['semantic_fingerprint']
    run_html(tmp_path, '<button>Save</button>', check)


def test_navigation_retires_old_references_even_for_same_content(tmp_path, web_app):
    async def run():
        engine = LocalTools(tmp_path, Artifacts(tmp_path / 'runs'), headless=True)
        try:
            first = (await engine.execute('browser_open', {'url': web_app + '/cart'}))['result']
            second = (await engine.execute('browser_reload', {}))['result']
            assert second['screen_diff']['reset']
            assert second['observation_id'] != first['observation_id']
            assert second['semantic_fingerprint'] == first['semantic_fingerprint']
            result = await engine.execute('browser_click_node', reference(first, 'Add item'))
            assert result['result']['code'] == 'STALE_OBSERVATION'
        finally:
            await engine.close()
    asyncio.run(run())


def test_references_cannot_cross_browser_contexts(tmp_path):
    async def check(engine, page, first):
        other = LocalTools(tmp_path, Artifacts(tmp_path / 'other'), headless=True)
        try:
            await (await other.browser_page()).set_content('<button>Save</button>')
            await snapshot(other)
            result = await other.execute('browser_click_node', reference(first, 'Save'))
            assert result['result']['code'] == 'STALE_OBSERVATION'
        finally:
            await other.close()
    run_html(tmp_path, '<button>Save</button>', check)


def test_fill_press_and_unique_locator_translation(tmp_path):
    html = '<label>Name<input onkeydown="if(event.key===\'Enter\')document.querySelector(\'p\').textContent=this.value"></label><p></p>'
    async def check(engine, page, first):
        filled = await engine.execute('browser_fill_node', {**reference(first, 'Name'), 'value': 'Ada'})
        assert filled['ok'], filled
        assert filled['result']['resolved_action']['tool'] == 'browser_fill'
        pressed = await engine.execute('browser_press_node', {**reference(filled['result'], 'Name'), 'key': 'Enter'})
        assert pressed['ok'], pressed
        assert await page.locator('p').inner_text() == 'Ada'
        assert pressed['result']['resolved_action']['arguments']['key'] == 'Enter'
    run_html(tmp_path, html, check)


def test_disabled_unknown_and_undeclared_node_actions_are_refused(tmp_path):
    async def check(engine, page, first):
        result = await engine.execute('browser_click_node', reference(first, 'Save'))
        assert result['result']['code'] == 'NODE_NOT_ACTIONABLE'
        result = await engine.execute('browser_click_node', {'node_id': 'n999', 'observation_id': first['observation_id']})
        assert result['result']['code'] == 'NODE_NOT_FOUND'
        result = await engine.execute('browser_click_node', {**reference(first, 'Save'), 'selector': 'button'})
        assert not result['ok'] and 'selector' in result['result']['error']
    run_html(tmp_path, '<button disabled>Save</button>', check)


def test_checkbox_state_change_appears_in_diff(tmp_path):
    async def check(engine, page, first):
        result = await engine.execute('browser_click_node', reference(first, 'Agree'))
        assert result['ok'], result
        change = next(c for c in result['result']['screen_diff']['changed'] if c['id'] == first['nodes'][0]['id'])
        assert change['before']['states']['checked'] == 'false'
        assert change['after']['states']['checked'] == 'true'
    run_html(tmp_path, '<input type="checkbox" aria-label="Agree">', check)


def test_open_shadow_dom_node_can_be_targeted(tmp_path):
    html = '''<div id="host"></div><p>Ready</p><script>document.querySelector('#host').attachShadow({mode:'open'}).innerHTML = '<button>Shadow</button>'; document.querySelector('#host').shadowRoot.querySelector('button').onclick=()=>document.querySelector('p').textContent='Clicked';</script>'''
    async def check(engine, page, first):
        result = await engine.execute('browser_click_node', reference(first, 'Shadow'))
        assert result['ok'], result
        assert await page.locator('p').inner_text() == 'Clicked'
    run_html(tmp_path, html, check)


def test_large_screen_explicitly_disables_reference_actions(tmp_path):
    async def check(engine, page, first):
        assert first['nodes_truncated'] and first['nodes'] == []
        assert first['semantic_fingerprint'] is None
        assert first['screen_diff']['truncated']
    run_html(tmp_path, '<button>Control</button>' * 121, check)


class NodeActor(Actor):
    async def act(self, context, *, on_call):
        on_call()
        self.contexts.append(context)
        if len(self.contexts) > 1:
            return complete()
        screen = context['observation']['result']
        return ActDecision.model_validate({'kind': 'action', 'action': {'tool': 'browser_click_node',
            'arguments': reference(screen, 'Add item'), 'reason': 'Use observed node'}})


def test_node_actions_export_and_cache_only_durable_locators(tmp_path, web_app):
    cache = ReplayCache(tmp_path / 'cache', tmp_path)
    async def run(actor):
        artifacts = Artifacts(tmp_path / 'runs')
        result = await JourneyRunner(LocalTools(tmp_path, artifacts, headless=True), artifacts,
            actor, replay_cache=cache, progress=lambda _: None).run(case(web_app, semantic=True))
        assert result.status == 'passed', result.detail
        return result, artifacts
    actor = NodeActor()
    first, original = asyncio.run(run(actor))
    cached = json.loads(next(cache.directory.glob('*.json')).read_text())
    action = cached['steps'][0]['actions'][0]['action']
    assert action['tool'] == 'browser_click'
    assert set(action['arguments']) == {'by', 'role', 'name'}
    assert 'observation_id' not in json.dumps(cached)
    assert 'node_id' not in json.dumps(cached)
    spec = json.loads((original.path / first.test_file).with_suffix('.json').read_text())
    assert spec['steps'][0]['kind'] == 'click'
    judge = NoActor()
    _, replayed = asyncio.run(run(judge))
    assert read_steps(replayed)[0]['cache'] == 'hit'
    assert read_steps(replayed)[0]['model_calls'] == 1
    assert set(judge.judgments[0][1]) <= {'url', 'snapshot', 'truncated'}
    assert 'screen_diff' not in judge.judgments[0][1]


def test_ambiguous_node_journey_passes_live_but_exports_barrier_and_bypasses_cache(tmp_path, web_app, monkeypatch):
    original_open = LocalTools.browser_open

    async def duplicated_open(engine, args):
        await original_open(engine, args)
        await engine.page.evaluate("const b=document.querySelector('button'); b.after(b.cloneNode(true))")
        return await engine.browser_snapshot(None)

    monkeypatch.setattr(LocalTools, 'browser_open', duplicated_open)
    cache = ReplayCache(tmp_path / 'cache', tmp_path)
    artifacts = Artifacts(tmp_path / 'runs')
    actor = NodeActor()
    result = asyncio.run(JourneyRunner(LocalTools(tmp_path, artifacts, headless=True), artifacts,
        actor, replay_cache=cache, progress=lambda _: None).run(case(web_app)))
    assert result.status == 'passed', result.detail
    assert read_steps(artifacts)[0]['cache'] == 'bypass'
    assert not list(cache.directory.glob('*.json'))
    spec = json.loads((artifacts.path / result.test_file).with_suffix('.json').read_text())
    assert spec['steps'][0]['kind'] == 'requires_verification'
    assert 'browser_click_node' in spec['steps'][0]['reason']
    assert 'node_id' not in json.dumps(spec) and 'observation_id' not in json.dumps(spec)
    assert actor.contexts[1]['observation']['result']['screen_diff']['counts']['text'] > 0


def test_unavailable_accessibility_retires_references_and_preserves_legacy_actions(tmp_path, monkeypatch):
    async def check(engine, page, first):
        original_send = engine.semantics.session.send

        async def unavailable(method, *args, **kwargs):
            if method == 'Accessibility.getFullAXTree':
                raise RuntimeError('CDP unavailable')
            return await original_send(method, *args, **kwargs)

        monkeypatch.setattr(engine.semantics.session, 'send', unavailable)
        failed = (await engine.execute('browser_snapshot', {}))['result']
        assert failed['snapshot'] and failed['semantic_error']
        assert failed['nodes_truncated'] and failed['nodes'] == []
        assert failed['observation_id'] is None
        stale = await engine.execute('browser_click_node', reference(first, 'Save'))
        assert stale['result']['code'] == 'STALE_OBSERVATION'
        assert not await page.evaluate('!!window.clicked')
        legacy = await engine.execute('browser_click', {'by': 'role', 'role': 'button', 'name': 'Save'})
        assert legacy['ok'] and await page.evaluate('window.clicked')
        monkeypatch.setattr(engine.semantics.session, 'send', original_send)
        restored = await snapshot(engine)
        assert restored['screen_diff']['reset']
        assert restored['observation_id'] != first['observation_id']
    run_html(tmp_path, '<button onclick="window.clicked=true">Save</button>', check)


def test_detached_after_binding_never_retargets_an_identical_replacement(tmp_path, monkeypatch):
    async def check(engine, page, first):
        original_bind = engine.semantics.bind

        async def detached(page, node_id):
            element = await original_bind(page, node_id)
            await element.evaluate('(node) => node.replaceWith(node.cloneNode(true))')
            return element

        monkeypatch.setattr(engine.semantics, 'bind', detached)
        result = await engine.execute('browser_click_node', reference(first, 'Save'))
        assert not result['ok']
        assert not await page.evaluate('!!window.clicked')
        assert 'resolved_action' not in result['result']
    run_html(tmp_path, '<button onclick="window.clicked=true">Save</button>', check)


def test_diff_is_bounded_independently_of_complete_current_table(tmp_path):
    async def check(engine, page, first):
        assert len(first['nodes']) == 40 and not first['nodes_truncated']
        assert first['screen_diff']['reset'] and first['screen_diff']['truncated']
        assert first['screen_diff']['counts']['added'] == 40
        assert len(first['screen_diff']['added']) == 30
        assert len(first['screen_diff']['text'][0]['after']) == 30
        current = await snapshot(engine)
        assert current['screen_diff']['unchanged'] and not current['screen_diff']['truncated']
    run_html(tmp_path, ''.join(f'<button>Control {i}</button>' for i in range(40)), check)


def test_readonly_node_fill_is_refused(tmp_path):
    async def check(engine, page, first):
        result = await engine.execute('browser_fill_node', {**reference(first, 'Name'), 'value': 'Changed'})
        assert result['result']['code'] == 'NODE_NOT_ACTIONABLE'
        assert await page.locator('input').input_value() == 'Original'
    run_html(tmp_path, '<input aria-label="Name" readonly value="Original">', check)


def test_cache_guards_semantic_state_but_not_transient_ids():
    from open_verify.replay_cache import screen_hash

    first = {'url': 'http://localhost/', 'snapshot': 'Button', 'semantic_fingerprint': 'before',
             'observation_id': 'one:1', 'nodes': [{'id': 'n1'}]}
    equivalent = {**first, 'observation_id': 'two:3', 'nodes': [{'id': 'n2'}]}
    assert screen_hash(first) == screen_hash(equivalent)
    assert screen_hash(first) != screen_hash({**first, 'semantic_fingerprint': 'after'})
    assert screen_hash({**first, 'nodes_truncated': True}) is None



def test_url_only_change_is_not_reported_as_an_unchanged_screen(tmp_path, web_app):
    async def run():
        engine = LocalTools(tmp_path, Artifacts(tmp_path / 'runs'), headless=True)
        try:
            first = (await engine.execute('browser_open', {'url': web_app + '/cart'}))['result']
            await engine.page.evaluate("history.pushState({}, '', '/cart?view=compact')")
            second = await snapshot(engine)
            assert first['nodes'] == second['nodes'] and first['snapshot'] == second['snapshot']
            assert first['observation_id'] != second['observation_id']
            diff = second['screen_diff']
            assert not diff['reset'] and not diff['unchanged']
            assert diff['url'] == {'before': first['url'], 'after': second['url']}
            result = await engine.execute('browser_click_node', reference(first, 'Add item'))
            assert result['result']['code'] == 'STALE_OBSERVATION'
        finally:
            await engine.close()
    asyncio.run(run())
