"""Deterministic execution and replay of saved HTTP/terminal test artifacts."""

import argparse
import asyncio
import shlex
import hashlib
import importlib.util
import json
import os
import re
import stat
from pathlib import Path
from uuid import uuid4

from open_verify import __version__
from open_verify.backend_spec import BackendTest
from open_verify.test_spec import TestResult

MAX_CHECK_BYTES = 1_000_000


class BackendBlocked(ValueError):
    """Required execution or complete assertion evidence is unavailable."""


def complete_text(root, receipt, field):
    """Read complete bounded UTF-8 evidence, never a truncated preview or outside path."""
    name = receipt.get(field)
    if not isinstance(name, str):
        raise BackendBlocked('Complete text evidence is unavailable')
    relative = Path(name)
    path = root / relative
    if relative.is_absolute() or '..' in relative.parts or not path.resolve().is_relative_to(root.resolve()):
        raise BackendBlocked('Text evidence must remain inside the run directory')
    try:
        if any(p.is_symlink() for p in (path, *path.parents) if p != root.parent and p.is_relative_to(root)):
            raise ValueError('Symlink evidence is not allowed')
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_CHECK_BYTES:
            raise ValueError('Expected a bounded regular evidence file')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as stream:
            after = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise ValueError('Evidence changed while opening')
            content = stream.read(MAX_CHECK_BYTES + 1)
        if len(content) > MAX_CHECK_BYTES:
            raise ValueError('Text evidence exceeds its limit')
        return content.decode('utf-8')
    except (OSError, ValueError) as exc:
        raise BackendBlocked('Text evidence is missing, invalid UTF-8 or exceeds 1000000 bytes') from exc


def check_text(actual, expected):
    """Apply a literal exact or substring check without evaluating expressions."""
    matches = actual == expected.value if expected.mode == 'equals' else expected.value in actual
    if not matches:
        raise AssertionError(f'Text {expected.mode} assertion failed')


def check_result(step, result, artifacts):
    """Use actual engine receipts for all expectations; failure never calls a model."""
    expected = step.expect
    if step.kind == 'command':
        if result.get('timed_out') or result.get('exit_code') is None:
            raise BackendBlocked('Command did not complete before its deadline')
        if type(result['exit_code']) is not int:
            raise BackendBlocked('Command exit evidence is invalid')
        if result['exit_code'] != expected.exit_code:
            raise AssertionError(f'Expected exit {expected.exit_code}, observed {result["exit_code"]}')
        if expected.output is not None:
            check_text(complete_text(artifacts.path, result, 'log'), expected.output)
        return
    if type(result.get('status')) is not int:
        raise BackendBlocked('HTTP status evidence is unavailable')
    if result['status'] != expected.status:
        raise AssertionError(f'Expected HTTP {expected.status}, observed {result["status"]}')
    if expected.headers:
        if not isinstance(result.get('headers'), dict):
            raise BackendBlocked('HTTP header evidence is unavailable')
        headers = {k.lower(): v for k, v in result['headers'].items()}
        for name, value in expected.headers.items():
            if headers.get(name.lower()) != value:
                raise AssertionError(f'HTTP header {name!r} did not match')
    if expected.text is None and expected.json_check is None:
        return
    if result.get('body_file_complete') is not True:
        raise BackendBlocked('HTTP body evidence is incomplete')
    body = complete_text(artifacts.path, result, 'body_file')
    if expected.text is not None:
        check_text(body, expected.text)
    if expected.json_check is not None:
        try:
            def reject_constant(value):
                raise ValueError('Non-finite JSON')
            value = json.loads(body, parse_constant=reject_constant)
            for key in expected.json_check.field:
                if isinstance(key, str) and isinstance(value, dict):
                    value = value[key]
                elif type(key) is int and isinstance(value, list):
                    value = value[key]
                else:
                    raise ValueError('JSON field has an incompatible type')
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise AssertionError('Response is not valid JSON or the expected field is absent') from exc
        def encode(item):
            return json.dumps(item, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if encode(value) != encode(expected.json_check.value):
            raise AssertionError('JSON value did not match expected value and type')


def preflight(test: BackendTest, engine):
    """Refuse known capability and policy gaps before any suite operation executes."""
    catalog = engine.catalog('execute')
    for step in test.steps:
        tool = 'http_request' if step.kind == 'http' else 'run_command'
        if tool not in catalog:
            raise BackendBlocked(f'Engine does not support {tool}')
        if step.kind == 'http':
            engine.check_url(step.url)
        elif not engine.allow_exec:
            raise BackendBlocked('Command execution requires --allow-exec')


async def execute_backend(test: BackendTest, engine, artifacts, *, progress=print):
    """Execute the saved typed sequence once, checkpointing failure and interruption."""
    results = []
    status, detail = 'blocked', 'Test did not start'
    index = None
    try:
        async with asyncio.timeout(test.timeout):
            preflight(test, engine)
            for index, step in enumerate(test.steps):
                first = len(artifacts.observations)
                progress(f'  Backend step {index + 1}/{len(test.steps)}: {step.kind}')
                if step.kind == 'command':
                    progress('    $ ' + shlex.join(step.argv))
                else:
                    progress(f'    {step.method} {step.url}')
                tool = 'http_request' if step.kind == 'http' else 'run_command'
                receipt = await engine.execute(tool, step.model_dump(exclude={'kind', 'expect'}))
                if not receipt['ok']:
                    raise BackendBlocked(str(receipt['result'].get('error', 'Engine action failed')))
                check_result(step, receipt['result'], artifacts)
                results.append({'index': index, 'status': 'passed', 'detail': 'All explicit assertions passed',
                                'evidence': [e['id'] for e in artifacts.observations[first:]]})
                artifacts.record('backend_assert', {'case_id': test.case_id}, results[-1], True)
            status, detail = 'passed', 'All backend assertions passed'
    except AssertionError as exc:
        status, detail = 'failed', str(exc)
    except TimeoutError:
        status, detail = 'blocked', 'Backend suite deadline exceeded'
    except asyncio.CancelledError:
        status, detail = 'blocked', 'Backend suite interrupted'
        raise
    except Exception as exc:
        status, detail = 'blocked', f'{type(exc).__name__}: {exc}'
    finally:
        if len(results) < len(test.steps):
            for remaining in range(len(results), len(test.steps)):
                active = remaining == index
                result = {'index': remaining, 'status': status if active else 'blocked',
                          'detail': detail if active else 'Not run after interruption, failure or preflight refusal',
                          'evidence': [e['id'] for e in artifacts.observations[first:]] if active else []}
                results.append(result)
                artifacts.record('backend_assert', {'case_id': test.case_id}, result, False)
        identity = hashlib.sha256(test.case_id.encode()).hexdigest()[:16]
        artifacts.write(f'backend-{identity}.json', {'case_id': test.case_id, 'status': status,
                        'detail': detail, 'steps': results})
    return {'status': status, 'detail': detail}


def write_backend_support(folder):
    """Keep backend instructions alongside browser support when both share a bundle."""
    requirements = folder / 'requirements.txt'
    if not requirements.exists():
        requirements.write_text(f'open-verify=={__version__}\n', encoding='utf-8')
    readme = folder / 'README.md'
    previous = readme.read_text() if readme.exists() else ''
    if '# Generated HTTP and terminal tests' not in previous:
        readme.write_text(previous + '\n# Generated HTTP and terminal tests\n\n'
            'Install the pinned Open Verify base package (from standalone source if unpublished). '
            'No browser, agent or Git is needed. Start required services and install project dependencies '
            'before replay. Run the manifest argv from the bundle root; set --project to the checkout '
            'under test and --allow-exec for commands. --allow-origin permits external HTTP origins. '
            'No commands or services run automatically during setup. SPEC in each Python file is '
            'editable typed data; replay validates it again. Requests do not follow redirects. '
            'Tests execute their operations again and may change application data. Use disposable data '
            'and never put credentials in exported URLs, headers, bodies, argv or stdin. '
            'Exit codes: 0 passed, 1 assertion failed, 2 blocked, 130 interrupted. '
            'Replay diagnostics are written below --output (default verification-replay).\n', encoding='utf-8')


def save_backend_test(test: BackendTest, artifacts):
    """Only compiler-owned Python syntax runs; all proposed values remain JSON literals."""
    identity = hashlib.sha256(test.case_id.encode()).hexdigest()[:16]
    folder = artifacts.path / 'tests'
    folder.mkdir(exist_ok=True)
    path = folder / f'test_backend_{identity}.py'
    source = ('"""Generated backend regression test. Start required services before replay."""\n'
        'import json\nfrom open_verify.backend_spec import BackendTest\n'
        'from open_verify.backend_runner import execute_backend, replay_main\n\n'
        f'SPEC = json.loads({test.model_dump_json()!r})\n\n'
        'async def test_backend(engine, artifacts, *, progress=print):\n'
        '    return await execute_backend(BackendTest.model_validate(SPEC), engine, artifacts, progress=progress)\n\n'
        "if __name__ == '__main__':\n    replay_main(test_backend)\n")
    path.write_text(source, encoding='utf-8')
    path.with_suffix('.json').write_text(test.model_dump_json(indent=2), encoding='utf-8')
    write_backend_support(folder)
    return path


def embed_generated_scripts(test: BackendTest, engine, artifacts) -> BackendTest:
    """Carry stdin-created Python helpers in the executed/exported specification.

    Only copy a regular file whose exact contents were supplied as command stdin
    in this run. Product scripts and arbitrary local files are not bundled.
    Execute the embedded helper from scratch storage to preserve __file__ and
    child-process usage without relying on the original disposable checkout.
    """
    saved = test.model_copy(deep=True)
    sources = {e.get('arguments', {}).get('stdin') for e in artifacts.observations
               if e['tool'] == 'run_command' and e['ok']}
    sources.discard(None)
    sources.discard('')
    for step in saved.steps:
        if step.kind != 'command':
            continue
        for index, word in enumerate(step.argv[:-1]):
            if not re.fullmatch(r'python(?:\d+(?:\.\d+)*)?(?:\.exe)?', Path(word).name):
                continue
            argument = step.argv[index + 1]
            if not argument.endswith('.py'):
                continue
            path = Path(argument)
            if not path.is_absolute():
                path = engine.project / step.cwd / path
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_size > 80_000:
                    continue
                source = path.read_text(encoding='utf-8')
            except (OSError, UnicodeError):
                continue
            if source not in sources:
                continue
            bootstrap = (
                'import pathlib, subprocess, sys, tempfile\n'
                'with tempfile.TemporaryDirectory(prefix="ov-replay-helper-") as folder:\n'
                f'    script = pathlib.Path(folder) / {path.name!r}\n'
                f'    script.write_text({source!r}, encoding="utf-8")\n'
                '    result = subprocess.run([sys.executable, str(script), *sys.argv[1:]])\n'
                'raise SystemExit(result.returncode)\n'
            )
            step.argv[index + 1:index + 2] = ['-c', bootstrap]
            break
    # Apply the same size and coverage validation to the final saved data.
    return BackendTest.model_validate(saved.model_dump())


class BackendRunner:
    """Execute the exact saved artifact with the host's existing checked engine."""

    def __init__(self, engine, artifacts, *, progress=print):
        self.engine, self.artifacts, self.progress = engine, artifacts, progress

    async def run(self, test: BackendTest, *, on_result=None):
        """Preserve the generated file and a blocked result even if execution is cancelled."""
        test = embed_generated_scripts(test, self.engine, self.artifacts)
        path = save_backend_test(test, self.artifacts)
        relative = path.relative_to(self.artifacts.path).as_posix()
        rerun = ['python', relative, '--project', str(self.engine.project)]
        if test.interface == 'terminal':
            rerun.append('--allow-exec')
        for origin in self.engine.environment().get('allowed_origins', []):
            rerun.extend(['--allow-origin', origin])
        outcome = {'status': 'blocked', 'detail': 'Backend execution interrupted or unavailable'}
        try:
            spec = importlib.util.spec_from_file_location('ov_backend_' + uuid4().hex, path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            outcome = await module.test_backend(self.engine, self.artifacts, progress=self.progress)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            outcome = {'status': 'blocked', 'detail': f'{type(exc).__name__}: {exc}'}
        finally:
            result = TestResult(case_id=test.case_id, runner=test.interface,
                test_file=relative, rerun=rerun, **outcome)
            if on_result:
                on_result(result)
        return result


def replay_main(test_function):
    """Replay with the same engine policies and no planner, browser or Git discovery."""
    from open_verify.artifacts import Artifacts
    from open_verify.local_engine import LocalEngine

    parser = argparse.ArgumentParser(description='Replay a generated HTTP/terminal suite')
    parser.add_argument('--project', type=Path, default=Path.cwd())
    parser.add_argument('--allow-exec', action='store_true')
    parser.add_argument('--allow-origin', action='append', default=[])
    parser.add_argument('--output', type=Path, default=Path.cwd() / 'verification-replay')
    args = parser.parse_args()
    if not args.project.is_dir():
        parser.error('--project must be an existing directory')

    async def run():
        artifacts = Artifacts(args.output.resolve())
        engine = LocalEngine(args.project.resolve(), artifacts, allow_exec=args.allow_exec,
                             allow_origins=args.allow_origin)
        outcome = {'status': 'blocked', 'detail': 'Replay interrupted'}
        try:
            outcome = await test_function(engine, artifacts)
        except Exception as exc:
            outcome = {'status': 'blocked', 'detail': f'{type(exc).__name__}: {exc}'}
        finally:
            errors = await engine.close()
            if errors:
                outcome = {'status': 'blocked', 'detail': 'Cleanup failed: ' + '; '.join(errors)}
            artifacts.write('replay.json', outcome)
        print(json.dumps(outcome))
        return {'passed': 0, 'failed': 1, 'blocked': 2}[outcome['status']]
    try:
        raise SystemExit(asyncio.run(run()))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
