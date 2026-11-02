const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {scan, findings} = require('./duplication.cjs');
const config = require('./duplication.json');
const block = Array.from({length: 20}, (_, i) => `result_${i} = process(value_${i}, ${i})`).join('\n');
const other = Array.from({length: 20}, (_, i) => `different_${i} = alternate(input_${i}, ${i + 50})`).join('\n');
function tree(t, files, options = config) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'clone-test-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  for (const [file, source] of Object.entries(files)) {
    fs.mkdirSync(path.dirname(path.join(root, file)), {recursive: true});
    fs.writeFileSync(path.join(root, file), source);
  }
  return scan(root, options);
}
function copies(names, source = block) { return Object.fromEntries(names.map(n => [`${n}.py`, source])); }
test('third occurrence and fourth occurrence flag every matching file with context', t => {
  const two = tree(t, copies(['a', 'b']));
  const three = tree(t, copies(['a', 'b', 'c']));
  assert.equal(findings(two, new Map()).length, 0);
  const result = findings(three, two);
  assert.ok(result.length);
  assert.deepEqual(result[0].locations.map(l => l.file), ['a.py', 'b.py', 'c.py']);
  assert.match(result[0].context, /process/);
  assert.ok(findings(tree(t, copies(['a', 'b', 'c', 'd'])), three).length);
});
test('unrelated pairwise matches and multiple copies in one file do not qualify', t => {
  assert.equal(findings(tree(t, {'a.py': block, 'b.py': block + '\n' + other, 'c.py': other}), new Map()).length, 0);
  assert.equal(findings(tree(t, {'a.py': [block, block, block].join('\n')}), new Map()).length, 0);
});
test('unchanged debt, shifted locations and normalized whitespace/comments do not block', t => {
  const base = tree(t, copies(['a', 'b', 'c']));
  const shifted = tree(t, {...copies(['a', 'b', 'c']), 'a.py': '# comment\n\n' + block.replaceAll(' = ', '  =   '), 'unrelated.py': 'x = 1'});
  assert.equal(findings(shifted, base).length, 0);
  assert.ok([...shifted.values()].some(g => g.locations.some(l => l.file === 'a.py' && l.start === 3)));
});
test('changed shared content is a new group; added copies within qualifying files expand debt', t => {
  const base = tree(t, copies(['a', 'b', 'c']));
  assert.ok(findings(tree(t, copies(['a', 'b', 'c'], other)), base).length);
  assert.ok(findings(tree(t, {...copies(['a', 'b', 'c']), 'a.py': block + '\n' + block}), base).length);
});
test('small boilerplate, dependencies, generated output and detector fixtures are excluded', t => {
  assert.equal(findings(tree(t, copies(['a', 'b', 'c'], 'import os\nx = 1')), new Map()).length, 0);
  for (const directory of ['node_modules', 'dist', 'generated', 'fixtures', 'vendor', '.venv']) {
    assert.equal(findings(tree(t, {...copies(['a', 'b']), [`${directory}/c.py`]: block}), new Map()).length, 0);
  }
  assert.ok(config.minTokens >= 100);
});
test('exceptions require rationale and exact content/file/count scope; fourth file is not waived', t => {
  const three = tree(t, copies(['a', 'b', 'c']));
  const exceptions = findings(three, new Map()).map(g => ({id: g.id, files: {'a.py': 1, 'b.py': 1, 'c.py': 1}, rationale: 'Independent protocol entrypoints; reviewed responsibility.'}));
  assert.equal(findings(three, new Map(), exceptions).length, 0);
  assert.ok(findings(tree(t, copies(['a', 'b', 'c', 'd'])), new Map(), exceptions).length);
  assert.ok(findings(tree(t, copies(['a', 'b', 'c'], other)), new Map(), exceptions).length);
  assert.ok(findings(tree(t, {...copies(['a', 'b', 'c']), 'a.py': block + '\n' + block}), new Map(), exceptions).length);
  assert.throws(() => findings(three, new Map(), [{id: exceptions[0].id, files: {'*.py': 1}}]));
});

test('minimum size is configurable and invalid values fail closed', t => {
  const files = copies(['a', 'b', 'c'], 'value = process(input)');
  assert.equal(findings(tree(t, files), new Map()).length, 0);
  assert.ok(findings(tree(t, files, {...config, minTokens: 5}), new Map()).length);
  assert.throws(() => tree(t, files, {...config, minTokens: 0}));
});
test('JavaScript sources also use token-aware grouping', t => {
  const code = Array.from({length: 30}, (_, i) => `const item${i} = transform(value${i}, ${i});`).join('\n');
  const files = Object.fromEntries(['a.js', 'b.js', 'c.js'].map(n => [n, code]));
  assert.ok(findings(tree(t, files), new Map()).length);
});

test('relocated copies preserve debt, while expansion after relocation still fails', t => {
  const base = tree(t, copies(['a', 'b', 'c']));
  assert.equal(findings(tree(t, copies(['a', 'b', 'renamed'])), base).length, 0);
  assert.equal(findings(tree(t, copies(['moved/a', 'moved/b', 'moved/c'])), base).length, 0);
  assert.ok(findings(tree(t, copies(['a', 'b', 'renamed', 'fourth'])), base).length);
  assert.ok(findings(tree(t, {...copies(['a', 'b', 'renamed']), 'a.py': block + '\n' + block}), base).length);
  const concentrated = tree(t, {...copies(['a', 'b']), 'a.py': block + '\n' + block});
  assert.ok(findings(tree(t, copies(['a', 'b', 'c'])), concentrated).length);
});
test('retained groups preserve all locations and representative context', t => {
  const groups = tree(t, {'a.py': block + '\n' + block, 'b.py': block, 'unique.py': other});
  assert.ok(groups.size);
  for (const group of groups.values()) {
    assert.deepEqual([...new Set(group.locations.map(l => l.file))], ['a.py', 'b.py']);
    assert.equal(group.locations.length, 3);
    assert.match(group.context, /process/);
    assert.doesNotMatch(group.context, /alternate/);
  }
});
