# /// script
# requires-python = ">=3.11"
# dependencies = ["ladybug==0.22.1"]
# ///
"""Load the repository's Python source into a LadybugDB graph and visualize it.

The graph has one node per distribution (``Package``), module, class and
function, joined by containment, import, inheritance and package-dependency
relationships. Everything the report shows is read back out of the database
with Cypher, so the queries in ``REPORT_QUERIES`` double as examples for ad hoc
exploration::

    uv run scripts/code_graph.py build            # parse source into .code-graph/db
    uv run scripts/code_graph.py report           # write .code-graph/report.html
    uv run scripts/code_graph.py query "MATCH (m:Module) RETURN m.name, m.loc ORDER BY m.loc DESC LIMIT 5"

``build`` replaces the database each time; it takes a few seconds.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
from decimal import Decimal
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shutil
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / '.code-graph'
TOOLING = 'repo-tooling'
SKIP_DIRS = {'.git', '.venv', 'node_modules', '__pycache__', 'dist', 'build', '.code-graph',
             '.pytest_cache', '.ruff_cache', '.mypy_cache'}

SCHEMA = [
    'CREATE NODE TABLE Package(name STRING PRIMARY KEY, path STRING, area STRING)',
    'CREATE NODE TABLE Module(name STRING PRIMARY KEY, path STRING, package STRING, area STRING,'
    ' loc INT64, is_test BOOLEAN)',
    'CREATE NODE TABLE Class(id STRING PRIMARY KEY, name STRING, module STRING, line INT64,'
    ' loc INT64, methods INT64)',
    'CREATE NODE TABLE Function(id STRING PRIMARY KEY, name STRING, module STRING, line INT64,'
    ' loc INT64, complexity INT64, is_async BOOLEAN, is_method BOOLEAN)',
    'CREATE REL TABLE CONTAINS(FROM Package TO Module)',
    'CREATE REL TABLE DEFINES(FROM Module TO Class, FROM Module TO Function, FROM Class TO Function)',
    'CREATE REL TABLE IMPORTS(FROM Module TO Module, names INT64, type_only BOOLEAN)',
    'CREATE REL TABLE INHERITS(FROM Class TO Class)',
    'CREATE REL TABLE DEPENDS_ON(FROM Package TO Package, imports INT64)',
]


# ---------------------------------------------------------------- extraction

@dataclass
class Graph:
    packages: list[dict] = field(default_factory=list)
    modules: list[dict] = field(default_factory=list)
    classes: list[dict] = field(default_factory=list)
    functions: list[dict] = field(default_factory=list)
    contains: list[dict] = field(default_factory=list)
    defines: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))
    imports: list[dict] = field(default_factory=list)
    inherits: list[dict] = field(default_factory=list)
    depends: list[dict] = field(default_factory=list)


def area_of(path: Path) -> str:
    """The top-level part of the repository a distribution lives in."""
    parts = path.parts
    if not parts:
        return 'migrations'  # The root distribution is engine-migrations.
    if parts[0] == 'packages' and len(parts) > 1 and parts[1] == 'adapters':
        return 'adapters'
    if parts[0] in {'scripts', 'workflows', 'e2e'}:
        return 'tooling'
    return parts[0]


def walk(root: Path):
    for directory, children, files in os.walk(root):
        children[:] = sorted(c for c in children if c not in SKIP_DIRS and not c.startswith('.'))
        yield Path(directory), sorted(files)


def discover(root: Path) -> tuple[dict[Path, str], list[Path]]:
    """Map each distribution directory to its project name, and list Python files."""
    distributions, sources = {}, []
    for directory, files in walk(root):
        if 'pyproject.toml' in files:
            project = tomllib.loads((directory / 'pyproject.toml').read_text()).get('project', {})
            if 'name' in project:
                distributions[directory.relative_to(root)] = project['name']
        sources.extend((directory / f).relative_to(root) for f in files if f.endswith('.py'))
    return distributions, sources


def owner(path: Path, distributions: dict[Path, str]) -> Path:
    for parent in path.parents:
        if parent in distributions:
            return parent
    return Path('.')


def module_name(path: Path, base: Path) -> str:
    """The dotted import name: everything after ``src/`` or else the distribution root."""
    parts = list(path.with_suffix('').parts)
    if 'src' in parts:
        parts = parts[len(parts) - parts[::-1].index('src'):]
    else:
        parts = parts[len(base.parts):]
    if parts and parts[-1] == '__init__':
        parts.pop()
    return '.'.join(parts) or path.parent.name


BRANCHES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.ExceptHandler,
            ast.With, ast.AsyncWith, ast.Assert, ast.comprehension, ast.match_case)


def complexity(node: ast.AST) -> int:
    """McCabe-style cyclomatic complexity, not descending into nested definitions."""
    score, stack = 1, list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(child, BRANCHES):
            score += 1
        elif isinstance(child, ast.BoolOp):
            score += len(child.values) - 1
        stack.extend(ast.iter_child_nodes(child))
    return score


def span(node: ast.AST) -> int:
    return (getattr(node, 'end_lineno', None) or node.lineno) - node.lineno + 1


def resolve(name: str, known: set[str]) -> str | None:
    """The longest known module that ``name`` is, or is inside."""
    while name:
        if name in known:
            return name
        name = name.rpartition('.')[0]
    return None


def type_checking(node: ast.If) -> bool:
    test = node.test
    return (isinstance(test, ast.Name) and test.id == 'TYPE_CHECKING') or (
        isinstance(test, ast.Attribute) and test.attr == 'TYPE_CHECKING')


def extract(root: Path, include_tests: bool) -> Graph:
    distributions, sources = discover(root)
    graph = Graph()
    for path, name in sorted(distributions.items()):
        graph.packages.append({'name': name, 'path': path.as_posix() or '.', 'area': area_of(path)})
    # The root distribution ships only migrations/; scripts, workflows and e2e beside
    # it belong to no distribution, so they get a synthetic one rather than inflating it.
    graph.packages.append({'name': TOOLING, 'path': '.', 'area': 'tooling'})

    trees: dict[str, tuple[ast.Module, dict]] = {}
    for source in sources:
        test = 'tests' in source.parts or source.name.startswith('test_') or source.name == 'conftest.py'
        if test and not include_tests:
            continue
        base = owner(source, distributions)
        try:
            tree = ast.parse((root / source).read_text(), filename=str(source))
        except (SyntaxError, UnicodeDecodeError) as error:
            print(f'skipping {source}: {error}', file=sys.stderr)
            continue
        name = module_name(source, base)
        if name in trees:  # Two distributions shipping the same module name; keep the first.
            continue
        package = distributions.get(base, TOOLING)
        if base == Path('.') and source.parts[0] != 'migrations':
            package = TOOLING
        module = {'name': name, 'path': source.as_posix(), 'package': package,
                  'area': area_of(base) if base != Path('.') else area_of(source),
                  'loc': len((root / source).read_text().splitlines()), 'is_test': test,
                  'is_package': source.name == '__init__.py'}
        trees[name] = (tree, module)
        graph.modules.append(module)
        graph.contains.append({'from': package, 'to': name})

    known = set(trees)
    class_ids: dict[str, str] = {}  # fully qualified class name -> node id
    for name, (tree, _) in trees.items():
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                class_ids[f'{name}.{node.name}'] = f'{name}.{node.name}'

    edges: dict[tuple[str, str], dict] = {}
    seen: set[str] = set()
    for name, (tree, module) in trees.items():
        aliases: dict[str, str] = {}  # local name -> fully qualified name
        anchor = name if module['is_package'] else name.rpartition('.')[0]

        def visit_imports(body, type_only=False):
            for node in body:
                if isinstance(node, ast.If) and type_checking(node):
                    visit_imports(node.body, True)
                    continue
                if isinstance(node, (ast.If, ast.Try)):
                    visit_imports(node.body, type_only)
                    for handler in getattr(node, 'handlers', []):
                        visit_imports(handler.body, type_only)
                    visit_imports(node.orelse, type_only)
                    continue
                targets = []
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        targets.append(alias.name)
                        aliases[(alias.asname or alias.name).split('.')[0]] = (
                            alias.name if alias.asname else alias.name.split('.')[0])
                elif isinstance(node, ast.ImportFrom):
                    base = node.module or ''
                    if node.level:
                        prefix = anchor.split('.')
                        prefix = prefix[:len(prefix) - node.level + 1]
                        base = '.'.join(filter(None, ['.'.join(prefix), base]))
                    for alias in node.names:
                        full = f'{base}.{alias.name}'
                        aliases[alias.asname or alias.name] = full
                        targets.append(full if full in known else base)
                for target in targets:
                    found = resolve(target, known)
                    if found and found != name:
                        edge = edges.setdefault((name, found), {'from': name, 'to': found,
                                                                'names': 0, 'type_only': True})
                        edge['names'] += 1
                        edge['type_only'] &= type_only

        visit_imports(tree.body)

        def qualify(expr: ast.expr) -> str | None:
            if isinstance(expr, ast.Name):
                return aliases.get(expr.id, f'{name}.{expr.id}')
            if isinstance(expr, ast.Attribute):
                head = qualify(expr.value)
                return f'{head}.{expr.attr}' if head else None
            if isinstance(expr, ast.Subscript):  # Generic[T], Protocol[T]
                return qualify(expr.value)
            return None

        def add_function(node, parent: str, is_method: bool):
            fid = f'{parent}.{node.name}'
            if fid in seen:  # @overload stubs, property setters, conditional redefinitions
                fid = f'{fid}@{node.lineno}'
            seen.add(fid)
            graph.functions.append({
                'id': fid, 'name': node.name, 'module': name, 'line': node.lineno,
                'loc': span(node), 'complexity': complexity(node),
                'is_async': isinstance(node, ast.AsyncFunctionDef), 'is_method': is_method})
            graph.defines['Class' if is_method else 'Module'].append({'from': parent, 'to': fid})

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                add_function(node, name, False)
            elif isinstance(node, ast.ClassDef):
                cid = f'{name}.{node.name}'
                if cid in seen:
                    continue
                seen.add(cid)
                methods = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                graph.classes.append({'id': cid, 'name': node.name, 'module': name,
                                      'line': node.lineno, 'loc': span(node), 'methods': len(methods)})
                graph.defines['ModuleClass'].append({'from': name, 'to': cid})
                for method in methods:
                    add_function(method, cid, True)
                for base in node.bases:
                    target = qualify(base)
                    # `from engine.ports import X` names engine.ports.X, which may be
                    # re-exported from a submodule; fall back to a unique short-name match.
                    if target and target not in class_ids:
                        short = target.rpartition('.')[2]
                        matches = [c for c in class_ids if c.endswith('.' + short)
                                   and c.startswith(target.rpartition('.')[0])]
                        target = matches[0] if len(matches) == 1 else None
                    if target and target in class_ids and target != cid:
                        graph.inherits.append({'from': cid, 'to': target})

    graph.imports = list(edges.values())
    owners = {m['name']: m['package'] for m in graph.modules}
    counts = Counter((owners[e['from']], owners[e['to']]) for e in graph.imports
                     if owners[e['from']] != owners[e['to']])
    graph.depends = [{'from': a, 'to': b, 'imports': n} for (a, b), n in sorted(counts.items())]
    return graph


# ------------------------------------------------------------------ database

def connect(path: Path):
    import ladybug
    return ladybug.Connection(ladybug.Database(str(path)))


def load(graph: Graph, path: Path) -> None:
    if path.exists():
        shutil.rmtree(path) if path.is_dir() else path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    for statement in SCHEMA:
        conn.execute(statement)

    def nodes(table: str, rows: list[dict], columns: list[str]):
        if rows:
            fields = ', '.join(f'{c}: r.{c}' for c in columns)
            conn.execute(f'UNWIND $rows AS r CREATE (:{table} {{{fields}}})',
                         {'rows': [{c: row[c] for c in columns} for row in rows]})

    def rels(rel: str, source: str, key_s: str, target: str, key_t: str, rows: list[dict],
             props: list[str] = ()):
        if rows:
            fields = ', '.join(f'{p}: r.{p}' for p in props)
            conn.execute(f'UNWIND $rows AS r MATCH (a:{source} {{{key_s}: r.f}}), (b:{target} {{{key_t}: r.t}})'
                         f' CREATE (a)-[:{rel} {{{fields}}}]->(b)',
                         {'rows': [{'f': row['from'], 't': row['to'], **{p: row[p] for p in props}}
                                   for row in rows]})

    nodes('Package', graph.packages, ['name', 'path', 'area'])
    nodes('Module', graph.modules, ['name', 'path', 'package', 'area', 'loc', 'is_test'])
    nodes('Class', graph.classes, ['id', 'name', 'module', 'line', 'loc', 'methods'])
    nodes('Function', graph.functions,
          ['id', 'name', 'module', 'line', 'loc', 'complexity', 'is_async', 'is_method'])
    rels('CONTAINS', 'Package', 'name', 'Module', 'name', graph.contains)
    rels('DEFINES', 'Module', 'name', 'Class', 'id', graph.defines['ModuleClass'])
    rels('DEFINES', 'Module', 'name', 'Function', 'id', graph.defines['Module'])
    rels('DEFINES', 'Class', 'id', 'Function', 'id', graph.defines['Class'])
    rels('IMPORTS', 'Module', 'name', 'Module', 'name', graph.imports, ['names', 'type_only'])
    rels('INHERITS', 'Class', 'id', 'Class', 'id', graph.inherits)
    rels('DEPENDS_ON', 'Package', 'name', 'Package', 'name', graph.depends, ['imports'])


def plain(value):
    """JSON for the values Ladybug returns that ``json`` does not know (``sum`` is a Decimal)."""
    return int(value) if isinstance(value, Decimal) and value == value.to_integral() else str(value)


def rows(conn, query: str, parameters: dict | None = None) -> list[dict]:
    result = conn.execute(query, parameters or {})
    columns = result.get_column_names()
    return [dict(zip(columns, row)) for row in result.get_all()]


# -------------------------------------------------------------------- report

REPORT_QUERIES = {
    'packages': """
        MATCH (p:Package)
        OPTIONAL MATCH (p)-[:CONTAINS]->(m:Module)
        RETURN p.name AS name, p.path AS path, p.area AS area,
               count(m) AS modules, coalesce(sum(m.loc), 0) AS loc""",
    'package_edges': """
        MATCH (a:Package)-[d:DEPENDS_ON]->(b:Package)
        RETURN a.name AS source, b.name AS target, d.imports AS weight""",
    'package_cycles': """
        MATCH (a:Package)-[:DEPENDS_ON]->(b:Package)-[:DEPENDS_ON]->(a)
        WHERE a.name < b.name
        RETURN a.name AS a, b.name AS b""",
    'modules': """
        MATCH (m:Module)
        OPTIONAL MATCH (m)-[:DEFINES]->(f:Function)
        WITH m, max(f.complexity) AS peak, count(f) AS functions
        OPTIONAL MATCH (m)-[:DEFINES]->(c:Class)
        RETURN m.name AS name, m.path AS path, m.package AS package, m.area AS area,
               m.loc AS loc, functions, count(c) AS classes, coalesce(peak, 0) AS peak""",
    'module_edges': """
        MATCH (a:Module)-[i:IMPORTS]->(b:Module)
        RETURN a.name AS source, b.name AS target, i.type_only AS type_only""",
    'fan_in': """
        MATCH (a:Module)-[:IMPORTS]->(b:Module)
        WHERE a.package <> b.package
        RETURN b.name AS module, b.package AS package, count(DISTINCT a.package) AS packages,
               count(a) AS importers
        ORDER BY packages DESC, importers DESC LIMIT 15""",
    'complex_functions': """
        MATCH (m:Module)-[:DEFINES*1..2]->(f:Function)
        RETURN f.id AS function, m.path AS path, f.line AS line, f.complexity AS complexity,
               f.loc AS loc
        ORDER BY complexity DESC LIMIT 15""",
    'large_classes': """
        MATCH (m:Module)-[:DEFINES]->(c:Class)
        RETURN c.id AS class, m.path AS path, c.line AS line, c.methods AS methods, c.loc AS loc
        ORDER BY methods DESC LIMIT 15""",
    'hierarchies': """
        MATCH (sub:Class)-[:INHERITS*1..6]->(base:Class)
        RETURN base.id AS base, count(DISTINCT sub) AS descendants
        ORDER BY descendants DESC LIMIT 15""",
    'totals': """
        MATCH (m:Module)
        RETURN count(m) AS modules, sum(m.loc) AS loc,
               count { MATCH (:Function) } AS functions, count { MATCH (:Class) } AS classes,
               count { MATCH (:Package) } AS packages,
               count { MATCH ()-[:IMPORTS]->() } AS imports""",
}


def report(db: Path, output: Path) -> None:
    conn = connect(db)
    data = {key: rows(conn, query) for key, query in REPORT_QUERIES.items()}
    data['totals'] = data['totals'][0]
    data['queries'] = {k: '\n'.join(line[8:] if line.startswith(' ' * 8) else line.strip()
                                    for line in v.strip('\n').splitlines())
                       for k, v in REPORT_QUERIES.items()}
    template = (Path(__file__).parent / 'code_graph_report.html').read_text()
    payload = json.dumps(data, default=plain).replace('</', '<\\/')
    output.write_text(template.replace('/*__DATA__*/null', payload))
    print(f'wrote {output}')


# ----------------------------------------------------------------------- cli

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', type=Path, default=OUTPUT / 'db', help='database path')
    commands = parser.add_subparsers(dest='command', required=True)
    build = commands.add_parser('build', help='parse the source tree into a fresh database')
    build.add_argument('--root', type=Path, default=ROOT)
    build.add_argument('--include-tests', action='store_true')
    out = commands.add_parser('report', help='render the HTML report from the database')
    out.add_argument('--output', type=Path, default=OUTPUT / 'report.html')
    query = commands.add_parser('query', help='run a Cypher query and print the rows as JSON lines')
    query.add_argument('cypher')
    args = parser.parse_args()

    if args.command == 'build':
        graph = extract(args.root.resolve(), args.include_tests)
        load(graph, args.db)
        print(f'loaded {len(graph.packages)} packages, {len(graph.modules)} modules, '
              f'{len(graph.classes)} classes, {len(graph.functions)} functions, '
              f'{len(graph.imports)} imports into {args.db}')
    elif args.command == 'report':
        report(args.db, args.output)
    else:
        for row in rows(connect(args.db), args.cypher):
            print(json.dumps(row, default=plain))


if __name__ == '__main__':
    main()
