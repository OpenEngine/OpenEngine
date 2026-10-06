"""Regression coverage for recursive checks, redirected exports and cleanup cancellation."""

import asyncio
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from test_journeys import Actor, case, complete
from test_replay_cache import Engine, NoActor, entries, execute
from test_tools import web_app as web_app

import open_verify
from open_verify.artifacts import Artifacts
from open_verify.journey import JourneyRunner
from open_verify.journey_spec import ActDecision
from open_verify.replay_cache import ReplayCache
from open_verify.test_codegen import render_test
from open_verify.test_spec import BrowserTest, ExpectJSON, ExpectText, ExpectURL
from open_verify.tools import LocalTools


@pytest.mark.parametrize(('field', 'expected', 'passes'), [
    ([], {'items': [True, 2]}, False),
    (['items'], [True, 2], False),
    ([], {'items': [1, 2]}, True),
    (['items'], [1, 2], True),
])
def test_nested_json_matches_in_live_and_generated_assertions(tmp_path, web_app, field, expected, passes):
    async def run():
        tools = LocalTools(tmp_path, Artifacts(tmp_path / 'runs'), headless=True)
        check = ExpectJSON(kind='expect_json', path='/api', field=field, value=expected)
        try:
            await tools.execute('browser_open', {'url': web_app})
            receipt = await tools.assert_check(check)
            assert receipt['result']['status'] == ('passed' if passes else 'failed')
            test = BrowserTest(case_id='json', url=web_app, steps=[check])
            namespace = {'__name__': 'generated_journey'}
            exec(compile(render_test(test), '<generated>', 'exec'), namespace)
            page = await tools.browser_page()
            if passes:
                await namespace['test_change'](page)
            else:
                with pytest.raises(AssertionError, match='JSON field did not match'):
                    await namespace['test_change'](page)
        finally:
            await tools.close()
    asyncio.run(run())


@pytest.mark.parametrize('visible', [True, False])
def test_duplicate_exact_text_checks_match_live_and_export(tmp_path, web_app, visible):
    async def run():
        tools = LocalTools(tmp_path, Artifacts(tmp_path / 'runs'), headless=True)
        check = ExpectText(kind='expect_text', text='Unique dummy prompt', visible=visible)
        try:
            await tools.execute('browser_open', {'url': web_app})
            page = await tools.browser_page()
            style = '' if visible else ' style="display:none"'
            html = ('<span hidden>Unique dummy prompt</span>'
                    f'<h1{style}>Unique dummy prompt</h1><p{style}>Unique dummy prompt</p>')
            await page.set_content(html)
            receipt = await tools.assert_check(check)
            assert receipt['result']['status'] == 'passed'
            namespace = {'__name__': 'generated_journey'}
            test = BrowserTest(case_id='duplicate', url=web_app, steps=[check])
            exec(compile(render_test(test), '<generated>', 'exec'), namespace)
            # Preserve a fresh fixture on the generated test's entry navigation.
            await page.route(web_app, lambda route: route.fulfill(body=html, content_type='text/html'))
            await namespace['test_change'](page)
            if visible:
                assert (await tools.execute('browser_expect_text', {'text': check.text}))['ok']
        finally:
            await tools.close()
    asyncio.run(run())


def test_export_waits_for_creation_navigation_before_recording_dynamic_url(tmp_path, web_app):
    async def run():
        tools = LocalTools(tmp_path, Artifacts(tmp_path / 'runs'), headless=True)
        try:
            page = await tools.browser_page()
            html = ('<p>Dummy prompt</p><button onclick="setTimeout(() => '
                    'location.href = \'/record/new-id\', 150)">Create</button>')
            await page.route(web_app + '/new', lambda route: route.fulfill(body=html, content_type='text/html'))
            await page.route(web_app + '/record/new-id', lambda route: route.fulfill(
                body='<h1>Dummy prompt</h1>', content_type='text/html'))
            test = BrowserTest(case_id='identity', url=web_app + '/new', steps=[
                {'kind': 'click', 'locator': {'by': 'role', 'role': 'button', 'name': 'Create'}},
                {'kind': 'expect_text', 'text': 'Dummy prompt'},
                {'kind': 'remember_url', 'name': 'created', 'wait_for_navigation': True},
                {'kind': 'reload'},
                {'kind': 'expect_same_url', 'baseline': 'created'}])
            namespace = {'__name__': 'generated_journey'}
            exec(compile(render_test(test), '<generated>', 'exec'), namespace)
            await namespace['test_change'](page)
            assert page.url == web_app + '/record/new-id'
        finally:
            await tools.close()
    asyncio.run(run())


@pytest.fixture
def redirected_origins():
    requests = []

    class App(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.server.label, self.path))
            if self.server.label == 'A' and self.path == '/cart':
                self.send_response(302)
                self.send_header('Location', origin_b + '/landing')
                self.end_headers()
                return
            body = f'<p>Origin {self.server.label}</p>'.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    servers = [ThreadingHTTPServer(('127.0.0.1', 0), App) for _ in range(2)]
    origin_a, origin_b = [f'http://127.0.0.1:{server.server_port}' for server in servers]
    threads = []
    for label, server in zip(('A', 'B'), servers, strict=True):
        server.label = label
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        threads.append(thread)
    try:
        yield origin_a, origin_b, requests
    finally:
        for server, thread in zip(servers, threads, strict=True):
            server.shutdown()
            server.server_close()
            thread.join()


def test_export_preserves_absolute_navigation_after_entry_redirect(tmp_path, redirected_origins):
    origin_a, _, requests = redirected_origins
    destination = origin_a + '/done?query=one%20two#result'
    actor = Actor([ActDecision.model_validate({'kind': 'action', 'action': {
        'tool': 'browser_open', 'arguments': {'url': destination}, 'reason': 'Return to A'}}), complete()])
    target = case(origin_a)
    target.journey.steps[1].check = ExpectURL(kind='expect_url', url=destination)
    artifacts = Artifacts(tmp_path / 'runs')
    result = asyncio.run(JourneyRunner(LocalTools(tmp_path, artifacts, headless=True), artifacts,
        actor, progress=lambda _: None).run(target))
    assert result.status == 'passed', result.detail
    assert ('B', '/landing') in requests
    requests.clear()
    replay = subprocess.run([sys.executable, result.test_file], cwd=artifacts.path,
        env={**os.environ, 'PYTHONPATH': str(Path(open_verify.__file__).resolve().parent.parent)},
        capture_output=True, text=True, timeout=30)
    assert replay.returncode == 0, replay.stdout + replay.stderr
    assert ('A', '/done?query=one%20two') in requests
    assert ('B', '/done?query=one%20two') not in requests


@pytest.mark.parametrize(('warm', 'mode'), [(False, 'auto'), (True, 'auto'), (True, 'strict'), (True, 'refresh')])
def test_cleanup_cancellation_checkpoints_results_and_finalizes_cache(tmp_path, warm, mode):
    cache = ReplayCache(tmp_path / 'cache', tmp_path)
    if warm:
        assert execute(tmp_path, cache)[0].status == 'passed'
    artifacts = Artifacts(tmp_path / 'runs')
    callbacks = []
    cache_at_callback = []

    async def run():
        closing = asyncio.Event()

        class CancellingEngine(Engine):
            async def close(self):
                closing.set()
                await asyncio.Event().wait()

        actor = NoActor() if warm and mode != 'refresh' else Actor()
        runner = JourneyRunner(CancellingEngine(artifacts), artifacts, actor,
            replay_cache=cache, cache_mode=mode, progress=lambda _: None)

        def checkpoint(result):
            callbacks.append(result)
            cache_at_callback.append(len(entries(cache)))

        task = asyncio.create_task(runner.run(case('http://localhost'), on_result=checkpoint))
        await asyncio.wait_for(closing.wait(), timeout=5)
        task.cancel('cancel during cleanup')
        with pytest.raises(asyncio.CancelledError, match='cancel during cleanup'):
            await task

    asyncio.run(run())
    assert len(callbacks) == 1
    result = callbacks[0]
    assert result.status == 'blocked'
    assert 'cleanup' in result.detail.lower()
    receipt = json.loads(next((artifacts.path / 'journeys').glob('*.json')).read_text())
    assert receipt['status'] == 'blocked'
    assert [step['status'] for step in receipt['steps']] == ['passed', 'passed']
    assert (artifacts.path / result.test_file).is_file()
    assert 'raise RuntimeError' in (artifacts.path / result.test_file).read_text()
    assert cache_at_callback == [1 if mode == 'strict' else 0]
    assert len(entries(cache)) == (1 if mode == 'strict' else 0)


@pytest.mark.parametrize(('actual', 'expected', 'equal'), [
    ({'enabled': 1}, {'enabled': True}, False),
    ({'enabled': True}, {'enabled': 1}, False),
    ([{'enabled': [0]}], [{'enabled': [False]}], False),
    ({'value': 1}, {'value': 1.0}, False),
    ({'value': 1}, {'value': '1'}, False),
    ({'value': None}, {'value': False}, False),
    ([1, 2], [1], False),
    ({'value': 1}, {'value': 1, 'other': None}, False),
    ({'b': [True, {'c': None}], 'a': 1}, {'a': 1, 'b': [True, {'c': None}]}, True),
    ([], [], True),
    ({}, {}, True),
])
def test_recursive_json_types_and_structure(actual, expected, equal):
    from open_verify.json_values import json_equal
    assert json_equal(actual, expected) is equal


@pytest.mark.parametrize('url', ['/done', '//example.com/done', 'javascript:alert(1)',
                                 'file:///tmp/test', 'https://user:password@example.com/done'])
def test_absolute_navigation_rejects_unsupported_destinations(url):
    from open_verify.test_spec import NavigateURL
    with pytest.raises(ValueError, match='Navigation requires'):
        NavigateURL(kind='navigate_url', url=url)


def test_cleanup_cancellation_remains_in_workflow_manifest(tmp_path):
    from test_architecture import FixtureExecutor
    from test_workflow import action, plan

    from open_verify.runner import VerificationRunner

    artifacts = Artifacts(tmp_path / 'runs')
    scripted = plan()
    scripted['plan']['cases'] = [case('http://localhost').model_dump()]
    runner = VerificationRunner(None, LocalTools(tmp_path, artifacts), artifacts,
        executor=FixtureExecutor([scripted, action('run_journey', case_id='cart')]), progress=lambda _: None)

    class CancellingEngine(Engine):
        async def close(self):
            raise asyncio.CancelledError('cleanup interrupted')

    runner.journeys.engine = CancellingEngine(artifacts)
    runner.journeys.executor = Actor()
    with pytest.raises(asyncio.CancelledError, match='cleanup interrupted'):
        asyncio.run(runner.run('Check cart'))
    manifest = json.loads((artifacts.path / 'manifest.json').read_text())
    assert manifest['status'] == 'incomplete'
    assert len(manifest['tests']) == 1
    assert manifest['tests'][0]['status'] == 'blocked'
    assert (artifacts.path / manifest['tests'][0]['path']).is_file()


def test_second_cancellation_during_cleanup_preserves_original_interruption(tmp_path):
    artifacts = Artifacts(tmp_path / 'runs')
    callbacks = []

    class CancellingActor(Actor):
        async def act(self, context, *, on_call):
            on_call()
            raise asyncio.CancelledError('original interruption')

    class CancellingEngine(Engine):
        async def close(self):
            raise asyncio.CancelledError('second interruption')

    runner = JourneyRunner(CancellingEngine(artifacts), artifacts, CancellingActor(), progress=lambda _: None)
    with pytest.raises(asyncio.CancelledError, match='original interruption'):
        asyncio.run(runner.run(case('http://localhost'), on_result=callbacks.append))
    assert len(callbacks) == 1
    assert callbacks[0].status == 'blocked'
    receipt = json.loads(next((artifacts.path / 'journeys').glob('*.json')).read_text())
    assert [step['code'] for step in receipt['steps']] == ['STEP_INTERRUPTED', 'NOT_RUN']
