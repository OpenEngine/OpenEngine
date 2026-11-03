"""The code graph extractor reads structure from source without importing it.

Loading into LadybugDB is left to the script's own ``uv run`` environment; the
extractor is plain ``ast`` and needs nothing beyond the standard library.
"""
import ast
from pathlib import Path

from scripts.code_graph import TOOLING, complexity, extract, module_name


def write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_extract_resolves_imports_inheritance_and_packages(tmp_path):
    write(tmp_path, 'pyproject.toml', '[project]\nname = "root"\n')
    write(tmp_path, 'packages/core/pyproject.toml', '[project]\nname = "core"\n')
    write(tmp_path, 'packages/core/src/acme/core/__init__.py', 'from .base import Base\n')
    write(tmp_path, 'packages/core/src/acme/core/base.py',
          'class Base:\n    def run(self):\n        return 1\n')
    write(tmp_path, 'packages/app/pyproject.toml', '[project]\nname = "app"\n')
    write(tmp_path, 'packages/app/src/acme/app/main.py', '\n'.join([
        'from typing import TYPE_CHECKING',
        'from acme.core import Base',
        'if TYPE_CHECKING:',
        '    from acme.core import base',
        'class Worker(Base):',
        '    def run(self):',
        '        return 2',
        '    def run(self):  # redefined',
        '        return 3',
        '']))
    write(tmp_path, 'packages/app/tests/test_main.py', 'import acme.app.main\n')
    write(tmp_path, 'scripts/tool.py', 'import acme.app.main\n')

    graph = extract(tmp_path, include_tests=False)

    modules = {m['name']: m for m in graph.modules}
    assert set(modules) == {'acme.core', 'acme.core.base', 'acme.app.main', 'scripts.tool'}
    assert modules['acme.app.main']['package'] == 'app'
    assert modules['scripts.tool']['package'] == TOOLING
    imports = {(e['from'], e['to']): e for e in graph.imports}
    assert imports[('acme.app.main', 'acme.core')]['type_only'] is False
    assert imports[('acme.app.main', 'acme.core.base')]['type_only'] is True
    assert ('acme.core', 'acme.core.base') in imports
    assert {'from': 'acme.app.main.Worker', 'to': 'acme.core.base.Base'} in graph.inherits
    assert {'from': 'app', 'to': 'core', 'imports': 2} in graph.depends
    ids = [f['id'] for f in graph.functions]
    assert len(ids) == len(set(ids)) and 'acme.app.main.Worker.run@8' in ids

    with_tests = extract(tmp_path, include_tests=True)
    assert 'tests.test_main' in {m['name'] for m in with_tests.modules}


def test_module_name_strips_src_layout_and_init():
    assert module_name(Path('packages/x/src/acme/x/__init__.py'), Path('packages/x')) == 'acme.x'
    assert module_name(Path('migrations/sqlite/env.py'), Path('.')) == 'migrations.sqlite.env'


def test_complexity_counts_branches_but_not_nested_definitions():
    function = ast.parse('\n'.join([
        'def f(a, b):',
        '    if a and b:',
        '        return [x for x in a if x]',
        '    def inner():',
        '        if a: pass',
        '    return 0',
    ])).body[0]
    assert complexity(function) == 4  # base, if, and, comprehension
