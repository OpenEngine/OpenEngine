const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {scan, findings} = require('./build/duplication.js');
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

test('npm prefix resolves both tree arguments from the invocation directory', t => {
  const {spawnSync} = require('node:child_process');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'clone-npm-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const current = path.join(root, 'current');
  const base = path.join(root, 'base');
  const quality = path.join(current, 'tools/quality');
  fs.mkdirSync(quality, {recursive: true});
  fs.mkdirSync(base);
  for (const name of ['a.py', 'b.py', 'c.py']) {
    fs.writeFileSync(path.join(current, name), block);
    if (name !== 'c.py') fs.writeFileSync(path.join(base, name), block);
  }
  // Invoke the real scanner without recursively running this test via the gate.
  fs.writeFileSync(path.join(quality, 'package.json'), JSON.stringify({
    scripts: {check: `node "${path.join(__dirname, 'build/duplication.js')}"`},
  }));
  const env = {...process.env};
  delete env.INIT_CWD;
  for (const args of [['../base', '.'], [base], [base, current]]) {
    const result = spawnSync('npm', ['--prefix', 'tools/quality', 'run', '--silent', 'check', '--', ...args],
      {cwd: current, env, encoding: 'utf8'});
    assert.equal(result.status, 1, result.stderr);
    const report = JSON.parse(result.stdout);
    assert.ok(report.findings.length);
    assert.deepEqual(report.findings[0].locations.map(l => l.file), ['a.py', 'b.py', 'c.py']);
  }
  const direct = spawnSync(process.execPath, [path.join(__dirname, 'build/duplication.js'), '../base', '.'],
    {cwd: current, env, encoding: 'utf8'});
  assert.equal(direct.status, 1, direct.stderr);
  assert.ok(JSON.parse(direct.stdout).findings.length);
});

test('all text files participate without an extension allowlist', t => {
  for (const extension of ['.rs', '.newlanguage', '']) {
    const files = Object.fromEntries(['a', 'b', 'c'].map(n => [n + extension, block]));
    const two = tree(t, Object.fromEntries(Object.entries(files).slice(0, 2)));
    const three = tree(t, files);
    assert.ok(findings(three, two).length, extension);
    assert.equal(findings(three, three).length, 0);
    assert.ok(findings(three, two)[0].context.includes('process'));
  }
});
test('binary files are skipped while text exclusions still apply', t => {
  for (const source of [Buffer.from(block + '\u0000'), Buffer.concat([Buffer.from(block), Buffer.from([255])])]) {
    assert.equal(tree(t, {'a.bin': source, 'b.bin': source, 'c.bin': source}).size, 0);
  }
  assert.equal(findings(tree(t, {'a.unknown': block, 'b.unknown': block, 'generated/c.unknown': block}), new Map()).length, 0);
});

test('rolling fingerprint collisions do not merge distinct token windows', t => {
  const words = Array.from({length: 32}, (_, i) => `word${i}`);
  // Interned IDs (1,32) and (2,1) have the same base-31 fingerprint.
  const groups = tree(t, {
    '0-seed.unknown': words.join(' '),
    'a.unknown': 'word0 word31',
    'b.unknown': 'word0 word31',
    'c.unknown': 'word1 word0',
  }, {...config, minTokens: 2});
  const {createHash} = require('node:crypto');
  const id = createHash('sha256').update(JSON.stringify(['text', ['word0', 'word31']])).digest('hex');
  assert.deepEqual(groups.get(id).locations.map(l => l.file), ['a.unknown', 'b.unknown']);
  assert.equal(findings(groups, new Map()).length, 0);
});

test('generated schema revisions are excluded without excluding other documentation', t => {
  for (const directory of ['docs/schema', 'v2/docs/schema']) {
    const base = tree(t, {...copies(['a', 'b']), [`${directory}/old/revision.py`]: block});
    const current = tree(t, {...copies(['a', 'b']),
      [`${directory}/old/revision.py`]: block, [`${directory}/new/revision.py`]: block});
    assert.equal(findings(current, base).length, 0);
    for (const group of current.values()) {
      assert.deepEqual(group.locations.map(l => l.file), ['a.py', 'b.py']);
    }
  }
  for (const directory of ['docs/guides', 'v2/docs/guides', 'docs/schema-examples']) {
    assert.ok(findings(tree(t, {...copies(['a', 'b']), [`${directory}/example.py`]: block}), new Map()).length);
  }
});


test('append-only tokenizer preserves pinned lexer tokens and locations', () => {
  const {tokenize} = require('./build/duplication.js');
  const {tokenize: original} = require('@jscpd/tokenizer');
  for (const [format, source] of [
    ['python', block + '\n# comment\n'],
    ['javascript', 'const a = `hello ${name}`;\n// comment\n'],
    ['markup', '<script>const a = 1;</script>\n<div>text</div>'],
  ]) {
    const project = tokens => tokens.map(({value, type, range, loc}) => ({value, type, range, start: loc.start.line, end: loc.end.line}));
    assert.deepEqual(project(tokenize(source, format)), project(original(source, format)));
  }
  const source = Array.from({length: 8000}, (_, i) => `value_${i} = process(${i})`).join('\n');
  const tokens = tokenize(source, 'python');
  assert.equal(tokens.at(-1).range[1], source.length);
  assert.equal(tokens.at(-1).loc.end.line, 8000);
});


test('recognized formats without loaded grammars use generic matching', t => {
  const {getFormatByFile} = require('@jscpd/tokenizer');
  for (const extension of ['.elm', '.cr']) {
    assert.ok(getFormatByFile('source' + extension));
    const files = Object.fromEntries(['a', 'b', 'c'].map(n => [n + extension, block]));
    const base = tree(t, Object.fromEntries(Object.entries(files).slice(0, 2)));
    const current = tree(t, {...files, 'unrelated.py': 'value = 1'});
    const result = findings(current, base);
    assert.ok(result.length, extension);
    assert.deepEqual(result[0].locations.map(l => l.file), ['a', 'b', 'c'].map(n => n + extension));
    assert.match(result[0].context, /process/);
    assert.equal(findings(current, current).length, 0);
  }
});

test('generated dependency lockfiles are excluded at every depth without excluding source', t => {
  for (const name of ['package-lock.json', 'uv.lock']) {
    const base = tree(t, {'a/source.unknown': block, 'b/source.unknown': block,
      ['old/' + name]: block});
    const current = tree(t, {'a/source.unknown': block, 'b/source.unknown': block,
      ['old/' + name]: block, ['nested/new/' + name]: block, [name]: block});
    assert.equal(findings(current, base).length, 0);
    for (const group of current.values()) {
      assert.deepEqual(group.locations.map(l => l.file), ['a/source.unknown', 'b/source.unknown']);
    }
  }
  for (const name of ['package.json', 'uv.lock.py', 'package-lock.json.example']) {
    const files = Object.fromEntries(['a', 'b', 'nested/c'].map(dir => [dir + '/' + name, block]));
    assert.ok(findings(tree(t, files), new Map()).length, name);
  }
});

test('HTML and JSX attribute whitespace neither hides clones nor creates new debt', t => {
  for (const extension of ['.html', '.jsx']) {
    const source = Array.from({length: 25}, (_, i) => `<div title="item${i}" id="entry${i}">value${i}</div>`).join('\n');
    const formatted = source.replaceAll(' title=', '\t  title=').replaceAll(' id=', '\n    id=');
    const files = value => Object.fromEntries(['a', 'b', 'c'].map(n => [n + extension, value]));
    const base = tree(t, files(source));
    assert.equal(findings(tree(t, files(formatted)), base).length, 0);
    const two = tree(t, Object.fromEntries(Object.entries(files(source)).slice(0, 2)));
    const result = findings(tree(t, {...files(source), ['c' + extension]: formatted}), two);
    assert.ok(result.length, extension);
    assert.deepEqual(result[0].locations.map(l => l.file), ['a', 'b', 'c'].map(n => n + extension));
  }
});
