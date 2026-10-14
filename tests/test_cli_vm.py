"""Local sandbox selection preserves configuration and refuses unusable VMs."""

import json
import stat
import tomllib

import pytest

from engine.adapters.sandbox.smolvm.support import SmolvmSupport
from engine.apps.cli import __main__ as cli, vm


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / 'engine.toml'
    path.write_text('# Keep this comment\n[server]\nport = 4364\n')
    path.chmod(0o600)
    monkeypatch.setenv('ENGINE_CONFIG', str(path))
    monkeypatch.setattr(vm.daemon, 'read_record', lambda: None)
    return path


def test_show_defaults_without_writing(config, capsys):
    original = config.read_bytes()
    assert cli.main(['vm', '--json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['backend'] == 'process' and result['changed'] is False
    assert config.read_bytes() == original


def test_switch_to_vm_and_back_preserves_settings_comments_and_permissions(config, tmp_path, monkeypatch, capsys):
    image = tmp_path / 'guest.tar'
    image.write_bytes(b'image')
    monkeypatch.setattr(vm, 'detect_support', lambda **_: SmolvmSupport(True, 'supported'))
    assert cli.main(['vm', 'smolvm', '--image', str(image)]) == 0
    settings = tomllib.loads(config.read_text())
    assert settings['sandbox'] == {'backend': 'smolvm', 'image': str(image)}
    assert settings['server']['port'] == 4364
    assert config.read_text().startswith('# Keep this comment')
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert 'engine daemon restart' in capsys.readouterr().out
    monkeypatch.setattr(vm, 'detect_support', lambda **_: pytest.fail('process selection must not probe SmolVM'))
    assert cli.main(['vm', 'process']) == 0
    assert tomllib.loads(config.read_text())['sandbox'] == {'backend': 'process', 'image': str(image)}


@pytest.mark.parametrize('failure', ['missing-image', 'empty-image', 'missing-binary'])
def test_vm_prerequisite_failures_preserve_configuration(config, tmp_path, monkeypatch, failure, capsys):
    original = config.read_bytes()
    image = tmp_path / 'guest.tar'
    if failure != 'missing-image':
        image.write_bytes(b'' if failure == 'empty-image' else b'image')
    monkeypatch.setattr(vm, 'detect_support', lambda **_: SmolvmSupport(False, 'smolvm is unavailable'))
    assert cli.main(['vm', 'smolvm', '--image', str(image)]) == 1
    assert config.read_bytes() == original
    assert 'engine vm:' in capsys.readouterr().err


def test_reuse_configured_image_resolves_relative_to_config(config, tmp_path, monkeypatch):
    (tmp_path / 'guest.tar').write_bytes(b'image')
    config.write_text('[sandbox]\nbackend = "process"\nimage = "guest.tar"\n')
    monkeypatch.setattr(vm, 'detect_support', lambda **_: SmolvmSupport(True, 'supported'))
    assert cli.main(['vm', 'smolvm']) == 0
    assert tomllib.loads(config.read_text())['sandbox']['image'] == str(tmp_path / 'guest.tar')


def test_vm_requires_image_without_changing_config(config):
    original = config.read_bytes()
    assert cli.main(['vm', 'smolvm']) == 1
    assert config.read_bytes() == original


@pytest.mark.parametrize('content', ['[sandbox]\nbackend = "unknown"\n', '[broken'])
def test_invalid_config_does_not_get_overwritten(config, content):
    config.write_text(content)
    assert cli.main(['vm', 'process']) == 1
    assert config.read_text() == content


def test_explicit_config_takes_precedence(config, tmp_path, capsys):
    other = tmp_path / 'other.toml'
    other.write_text('[sandbox]\nbackend = "smolvm"\nimage = "guest.tar"\n')
    assert cli.main(['vm', '--config', str(other), '--json']) == 0
    assert json.loads(capsys.readouterr().out)['backend'] == 'smolvm'


def test_image_is_refused_for_process_selection(config):
    original = config.read_bytes()
    assert cli.main(['vm', 'process', '--image', 'unused.tar']) == 1
    assert config.read_bytes() == original


def test_concurrent_config_change_is_not_overwritten(config):
    original = config.read_text()
    config.write_text('# A newer edit\n')
    with pytest.raises(ValueError, match='configuration changed'):
        vm._save(config, original, '[sandbox]\nbackend = "process"\n')
    assert config.read_text() == '# A newer edit\n'
    assert list(config.parent.iterdir()) == [config]


def test_vm_prerequisites_use_the_managed_service_path(config, tmp_path, monkeypatch):
    from types import SimpleNamespace

    image = tmp_path / 'guest.tar'
    image.write_bytes(b'image')
    service_path = '/service/tools:/usr/bin'
    monkeypatch.setattr(vm.daemon, 'read_record', lambda: SimpleNamespace(
        spec=SimpleNamespace(environment=lambda: {'PATH': service_path})))
    observed = []

    def support(*, path):
        observed.append(path)
        return SmolvmSupport(True, 'supported')

    monkeypatch.setattr(vm, 'detect_support', support)
    assert cli.main(['vm', 'smolvm', '--image', str(image)]) == 0
    assert observed == [service_path]


def test_failed_atomic_save_preserves_original_and_removes_temporary_file(config, monkeypatch):
    original = config.read_bytes()

    def refuse_replace(*_):
        message = 'replacement denied'
        raise OSError(message)

    monkeypatch.setattr(vm.os, 'replace', refuse_replace)
    assert cli.main(['vm', 'process']) == 1
    assert config.read_bytes() == original
    assert list(config.parent.iterdir()) == [config]


def test_missing_config_is_reported_without_creating_it(config):
    config.unlink()
    assert cli.main(['vm', 'process']) == 1
    assert not config.exists()


@pytest.mark.parametrize('backend', ['process', 'smolvm'])
def test_vm_list_marks_only_the_configured_backend(config, backend, capsys, monkeypatch):
    config.write_text(f'[sandbox]\nbackend = "{backend}"\n')
    original = config.read_bytes()
    monkeypatch.setattr(vm, 'detect_support', lambda **_: pytest.fail('listing must not probe or boot VMs'))
    assert cli.main(['vm', 'list']) == 0
    output = capsys.readouterr().out
    assert 'NAME' in output and 'DESCRIPTION' in output
    selected = [line.split() for line in output.splitlines() if line.startswith('*')]
    assert len(selected) == 1 and selected[0][1] == backend
    assert 'process' in output and 'smolvm' in output
    assert config.read_bytes() == original


@pytest.mark.parametrize('before', [True, False])
def test_vm_list_json_and_config_options_work_before_or_after_list(config, before, capsys):
    flags = ['--config', str(config), '--json']
    arguments = ['vm', *flags, 'list'] if before else ['vm', 'list', *flags]
    assert cli.main(arguments) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['current'] == 'process'
    assert [(row['name'], row['current']) for row in result['backends']] == [('process', True), ('smolvm', False)]


@pytest.mark.parametrize('before', [True, False])
def test_vm_use_switches_backend_and_list_reflects_it(config, tmp_path, monkeypatch, capsys, before):
    image = tmp_path / 'guest.tar'
    image.write_bytes(b'image')
    monkeypatch.setattr(vm, 'detect_support', lambda **_: SmolvmSupport(True, 'supported'))
    flags = ['--config', str(config), '--json', '--image', str(image)]
    args = ['vm', *flags, 'use', 'smolvm'] if before else ['vm', 'use', 'smolvm', *flags]
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)['backend'] == 'smolvm'
    assert cli.main(['vm', 'list', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['current'] == 'smolvm'
    assert cli.main(['vm', 'use', 'process', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['backend'] == 'process'


def test_vm_use_requires_backend_and_list_rejects_image(config):
    with pytest.raises(SystemExit) as error:
        cli.main(['vm', 'use'])
    assert error.value.code == 2
    original = config.read_bytes()
    assert cli.main(['vm', 'list', '--image', 'unused.tar']) == 1
    assert config.read_bytes() == original
