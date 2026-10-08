"""Generic readiness relates visible labels to fresh independently collected API identities."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pydantic import ValidationError

from open_verify.artifacts import Artifacts
from open_verify.journey import JourneyRunner
from open_verify.journey_spec import AssertStep, HTTPObservation, Judgment
from open_verify.models import Case
from open_verify.prompts import INSTRUCTIONS, JOURNEY_INSTRUCTIONS
from open_verify.tools import LocalTools


@pytest.fixture
def resource_app():
    state = {'label': 'Sample workspace', 'id': 'w-17', 'requests': 0, 'oversized': False}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/api/resources':
                state['requests'] += 1
                body = json.dumps({'resources': [{'name': state['label'], 'id': state['id']}],
                    'padding': 'x' * 25000 if state['oversized'] else ''}).encode()
            else:
                body = (f'<h1>Choose a resource</h1><label>Resource<select>'
                    f'<option>{state["label"]}</option></select></label>').encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def planned(url, label, identity):
    return Case.model_validate({'id': 'resource', 'title': 'Use a resource', 'interface': 'browser',
        'steps': ['Choose the resource'], 'expected': 'Resource is available',
        'journey': {'url': url + '/choose', 'readiness': [{
            'kind': 'assert', 'instruction': f'The visible {label} corresponds to id {identity}',
            'evidence_requests': [{'url': '/api/resources'}],
        }], 'steps': [{'kind': 'act', 'instruction': 'Use the resource'},
            {'kind': 'assert', 'instruction': 'Outcome appears'}]}})


@pytest.mark.parametrize('label,identity', [('Sample workspace', 'w-17'), ('Kitchen sensor', 'device-42')])
@pytest.mark.parametrize('matching', [True, False])
def test_readiness_uses_screen_and_fresh_api_without_guessing_identity(tmp_path, resource_app, label, identity, matching):
    url, state = resource_app
    state.update(label=label, id=identity if matching else 'different-id')
    artifacts = Artifacts(tmp_path / 'runs')
    engine = LocalTools(tmp_path, artifacts, headless=True)

    class Judge:
        calls = 0
        async def judge(self, instruction, observation, *, on_call):
            on_call()
            self.calls += 1
            assert label in observation['snapshot']
            assert identity not in observation['snapshot']
            assert 'completed_steps' not in observation and 'actor' not in observation
            source = observation['http_evidence'][0]
            assert source['url'] == url + '/api/resources' and source['status'] == 200
            resource = json.loads(source['body'])['resources'][0]
            assert resource['name'] == label
            return Judgment(verdict='holds' if resource['id'] == identity else 'fails',
                explanation='Visible label and API identity agree' if matching else 'The matching label has a different API identity')

    judge = Judge()
    async def run():
        try:
            return await JourneyRunner(engine, artifacts, judge,
                progress=lambda _: None).check_readiness(planned(url, label, identity))
        finally:
            await engine.close()

    result = asyncio.run(run())
    assert result['status'] == ('passed' if matching else 'blocked')
    assert judge.calls == 1 and state['requests'] == 1
    http = next(e for e in artifacts.observations if e['tool'] == 'http_request')
    assert http['id'] in result['checks'][0]['evidence']
    assert (artifacts.path / http['artifact']).is_file()


@pytest.mark.parametrize('fault', ['truncated', 'denied'])
def test_unavailable_required_api_evidence_cannot_be_ignored(tmp_path, resource_app, fault):
    url, state = resource_app
    state['oversized'] = fault == 'truncated'
    target = planned(url, state['label'], state['id'])
    if fault == 'denied':
        target.journey.readiness[0].evidence_requests = [HTTPObservation(url='https://not-authorized.invalid/data')]
    artifacts = Artifacts(tmp_path / 'runs')
    engine = LocalTools(tmp_path, artifacts, headless=True)

    class NoJudge:
        async def judge(self, *_args, **_kwargs):
            pytest.fail('A judge cannot waive missing required evidence')

    async def run():
        try:
            return await JourneyRunner(engine, artifacts, NoJudge(),
                progress=lambda _: None).check_readiness(target)
        finally:
            await engine.close()

    result = asyncio.run(run())
    assert result['status'] == 'blocked'
    assert result['checks'][0]['code'] == 'ASSERTION_EVIDENCE_UNAVAILABLE'
    assert state['requests'] == (1 if fault == 'truncated' else 0)


@pytest.mark.parametrize('url', ['file:///etc/passwd', '//other.invalid/a', '/\\other.invalid',
    'https://user:password@example.com/data', 'javascript:alert(1)', '/data#fragment'])
def test_declared_sources_reject_unsafe_or_ambiguous_urls(url):
    with pytest.raises(ValidationError):
        HTTPObservation(url=url)


@pytest.mark.parametrize('extra', [
    {'method': 'POST'}, {'headers': {'Authorization': 'secret'}}, {'body': 'mutate'},
])
def test_declared_sources_are_closed_read_only_requests(extra):
    with pytest.raises(ValidationError):
        HTTPObservation.model_validate({'url': '/api/resources', **extra})


@pytest.mark.parametrize('extra', [{'mode': 'visual'}, {'check': {'kind': 'expect_text', 'text': 'x'}}])
def test_evidence_cannot_be_silently_ignored_by_another_assertion_mode(extra):
    with pytest.raises(ValidationError, match='Additional HTTP evidence requires'):
        AssertStep.model_validate({'kind': 'assert', 'instruction': 'Evidence',
            'evidence_requests': [{'url': '/api/resources'}], **extra})


def test_core_guidance_does_not_prescribe_one_products_workflow():
    for term in ('WorkOrder', 'OpenEngine', 'reranker', 'complete_step', 'human-review', '/runs/new'):
        assert term not in INSTRUCTIONS + JOURNEY_INSTRUCTIONS
