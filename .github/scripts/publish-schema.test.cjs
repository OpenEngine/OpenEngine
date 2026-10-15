const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {test} = require('node:test');
const publish = require('./publish-schema.cjs');

test('publication skips unchanged/stale runs and commits only schema documents', async () => {
  const cwd = process.cwd();
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'schema-test-'));
  const calls = [];
  let head = 'source';
  const github = {
    rest: {
      git: {
        getRef: async () => ({data: {object: {sha: head}}}),
        getCommit: async () => ({data: {tree: {sha: 'old-tree'}}}),
        createTree: async args => { calls.push(args); return {data: {sha: 'tree'}}; },
        createCommit: async args => { calls.push(args); return {data: {sha: 'commit'}}; },
        updateRef: async args => { calls.push(args); },
      },
      pulls: {list: 'list', listFiles: 'files'},
      issues: {createComment: async args => { calls.push(args); }},
    },
    paginate: async method => method === 'files'
      ? [{filename: 'docs/schema/sqlite/revision.md', status: 'added'},
        {filename: 'docs/schema/README.md', status: 'added'}]
      : [{number: 12}],
  };
  const context = {repo: {owner: 'org', repo: 'repo'}, payload: {}, ref: 'refs/heads/feature', sha: 'source'};
  const core = {notice: () => {}};
  try {
    process.chdir(directory);
    fs.writeFileSync('schema-changes.json', '[]');
    await publish({github, context, core});
    assert.equal(calls.length, 0);
    fs.mkdirSync('docs/schema/sqlite', {recursive: true});
    fs.writeFileSync('docs/schema/sqlite/revision.md', '# Schema\n```diff\n+column\n```');
    fs.writeFileSync('schema-changes.json', '["sqlite/revision.md"]');
    head = 'newer-commit';
    await publish({github, context, core});
    assert.equal(calls.length, 0);
    head = 'source';
    await publish({github, context, core});
    assert.deepEqual(calls[0].tree.map(file => file.path), ['docs/schema/sqlite/revision.md']);
    assert.equal(calls[0].base_tree, 'old-tree');
    assert.deepEqual(calls[1].parents, ['source']);
    assert.equal(calls[2].force, false);
    assert.equal(calls[2].ref, 'heads/feature');
    assert.equal(calls[3].issue_number, 12);
    assert.match(calls[3].body, /```diff\n\+column/);
    assert.match(calls[3].body, /blob\/commit\/docs\/schema/);
    calls.length = 0;
    fs.writeFileSync('docs/schema/sqlite/revision.md', 'x'.repeat(60000));
    await publish({github, context, core});
    assert.ok(calls[3].body.length < 60000);
    assert.match(calls[3].body, /exceed the comment limit/);
    calls.length = 0;
    fs.writeFileSync('schema-changes.json', '[]');
    context.payload = {action: 'opened', pull_request: {
      number: 12, head: {ref: 'feature', sha: 'source'},
    }};
    await publish({github, context, core});
    assert.equal(calls.length, 1);
    assert.equal(calls[0].issue_number, 12);
    assert.match(calls[0].body, /blob\/source\/docs\/schema/);
  } finally {
    process.chdir(cwd);
    fs.rmSync(directory, {recursive: true, force: true});
  }
});

test('diagram PNG is uploaded as a binary blob and embedded in one PR comment', async () => {
  const cwd = process.cwd();
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'schema-png-test-'));
  const png = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
  let tree;
  const comments = [];
  const github = {
    rest: {
      git: {
        getRef: async () => ({data: {object: {sha: 'source'}}}),
        getCommit: async () => ({data: {tree: {sha: 'old-tree'}}}),
        createBlob: async args => {
          assert.equal(args.encoding, 'base64');
          assert.deepEqual(Buffer.from(args.content, 'base64'), png);
          return {data: {sha: 'png-blob'}};
        },
        createTree: async args => {tree = args.tree; return {data: {sha: 'tree'}};},
        createCommit: async () => ({data: {sha: 'commit'}}),
        updateRef: async () => {},
      },
      pulls: {list: 'list'},
      issues: {createComment: async args => comments.push(args)},
    },
    paginate: async () => [{number: 12}],
  };
  try {
    process.chdir(directory);
    fs.mkdirSync('docs/architecture', {recursive: true});
    fs.writeFileSync('docs/architecture/schema.png', png);
    fs.writeFileSync('docs/architecture/schema.mermaid', 'erDiagram\n');
    fs.writeFileSync('schema-changes.json', '[]');
    fs.writeFileSync('diagram-changes.json', JSON.stringify([
      'docs/architecture/schema.mermaid', 'docs/architecture/schema.png',
    ]));
    await publish({github, context: {repo: {owner: 'org', repo: 'repo'},
      payload: {}, ref: 'refs/heads/feature', sha: 'source'}, core: {notice: () => {}}});
    assert.equal(tree[1].sha, 'png-blob');
    assert.equal(tree[1].content, undefined);
    assert.equal(comments.length, 1);
    assert.match(comments[0].body, /!\[Schema changes\].*schema\.png\?raw=true/);
    assert.match(comments[0].body, /blob\/commit/);
  } finally {
    process.chdir(cwd);
    fs.rmSync(directory, {recursive: true, force: true});
  }
});
