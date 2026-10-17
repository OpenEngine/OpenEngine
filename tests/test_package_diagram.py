"""Package diagrams follow manifests without importing package code."""
import json

from scripts.package_diagram import diagram, packages


def test_discovers_nested_packages_and_normalizes_dependencies(tmp_path):
    manifests = {
        'packages/domain/pyproject.toml': '[project]\nname="Engine_Domain"\ndependencies=[]',
        'packages/adapters/store/sqlite/pyproject.toml': '''[project]
name="store"
dependencies=['engine.domain[extra]>=1; python_version >= "3.11"', 'external>=2']
[project.optional-dependencies]
local=['Engine_Domain']
''',
        'apps/web/package.json': json.dumps({'name': 'web', 'dependencies': {'shared': '*'}}),
        'npm/shared/package.json': json.dumps({'name': 'shared'}),
        'apps/web/node_modules/ignored/package.json': json.dumps({'name': 'ignored'}),
    }
    for path, content in manifests.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    current = packages(tmp_path)
    assert set(current) == {'python:engine-domain', 'python:store', 'npm:web', 'npm:shared'}
    output = diagram(current)
    assert output.count(' --> ') == 2
    assert output.count(' -.-> ') == 1
    assert 'packages/adapters/store/sqlite' in output
    assert 'external' not in output
    assert diagram(dict(reversed(list(current.items())))) == output
    (tmp_path / 'packages/domain/pyproject.toml').unlink()
    changed = diagram(packages(tmp_path))
    assert 'Engine_Domain' not in changed
    assert changed.count(' --> ') == 1


def test_manifest_skips_unchanged_output_and_recovers_missing_png(tmp_path, monkeypatch):
    from scripts.package_diagram import main

    output = tmp_path / 'docs'
    manifest = tmp_path / 'changes.json'
    monkeypatch.setattr('sys.argv', ['package_diagram', '--root', str(tmp_path),
                                   '--output', str(output), '--manifest', str(manifest)])
    main()
    expected = [str(output / 'packages.mermaid'), str(output / 'packages.png')]
    assert json.loads(manifest.read_text()) == expected
    (output / 'packages.png').write_bytes(b'PNG')
    main()
    assert json.loads(manifest.read_text()) == []
    (output / 'packages.png').unlink()
    main()
    assert json.loads(manifest.read_text()) == expected


def test_only_package_structure_changes_affect_diagram(tmp_path):
    app = tmp_path / 'apps/example'
    app.mkdir(parents=True)
    manifest = app / 'package.json'
    data = {'name': 'example', 'version': '1.0', 'dependencies': {'external': '1'}}
    manifest.write_text(json.dumps(data))
    original = diagram(packages(tmp_path))

    (app / 'index.js').write_text('// implementation changed')
    data.update(version='2.0', description='Updated description')
    data['dependencies']['external'] = '2'
    data['devDependencies'] = {'tool': '1'}
    manifest.write_text(json.dumps(data))
    assert diagram(packages(tmp_path)) == original

    shared = tmp_path / 'npm/shared'
    shared.mkdir(parents=True)
    (shared / 'package.json').write_text(json.dumps({'name': 'shared'}))
    added = diagram(packages(tmp_path))
    assert added != original

    data['dependencies']['shared'] = '*'
    manifest.write_text(json.dumps(data))
    linked = diagram(packages(tmp_path))
    assert linked != added
    assert linked.count(' --> ') == 1

    data['dependencies']['shared'] = '^2'
    manifest.write_text(json.dumps(data))
    assert diagram(packages(tmp_path)) == linked

    shared.rename(tmp_path / 'npm/relocated')
    assert diagram(packages(tmp_path)) != linked
    (tmp_path / 'npm/relocated/package.json').unlink()
    assert diagram(packages(tmp_path)) == original
