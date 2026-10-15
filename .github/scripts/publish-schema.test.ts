import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {test} from 'node:test';
import publish from './publish-schema.js';
import {parseManifest} from './schema-manifest.js';
import type {GitHub, Context, TreeEntry, Comment, CreateTree, CreateCommit, UpdateRef} from './schema-types.js';

test('publication skips unchanged/stale runs and commits only schema documents', async () => {
  const cwd = process.cwd();
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'schema-test-'));
  const calls: Array<CreateTree | CreateCommit | UpdateRef | Comment> = [];
  let head = 'source';
  const github: GitHub = {
    rest: {
      git: {
        createBlob: async () => {throw new Error('Unexpected binary upload');},
        getRef: async () => ({data: {object: {sha: head}}}),
        getCommit: async () => ({data: {tree: {sha: 'old-tree'}}}),
        createTree: async args => { calls.push(args); return {data: {sha: 'tree'}}; },
        createCommit: async args => { calls.push(args); return {data: {sha: 'commit'}}; },
        updateRef: async args => { calls.push(args); },
      },
      pulls: {list: async () => [{number: 12}], listFiles: async () => [
        {filename: 'docs/architecture/schema.mermaid', status: 'added'},
        {filename: 'docs/architecture/README.md', status: 'added'},
      ]},
      issues: {createComment: async args => { calls.push(args); }},
    },
    paginate: async (method, args) => {
      const response = await method(args);
      return Array.isArray(response) ? response : response.data;
    },
  };
  const context: Context = {repo: {owner: 'org', repo: 'repo'}, payload: {}, ref: 'refs/heads/feature', sha: 'source'};
  const core = {notice: () => {}};
  try {
    process.chdir(directory);
    fs.writeFileSync('diagram-changes.json', '[]');
    await publish({github, context, core});
    assert.equal(calls.length, 0);
    fs.mkdirSync('docs/architecture', {recursive: true});
    fs.writeFileSync('docs/architecture/schema.mermaid', 'erDiagram\n');
    fs.writeFileSync('docs/architecture/schema.png', 'png');
    fs.writeFileSync('diagram-changes.json', '["docs/architecture/schema.mermaid"]');
    head = 'newer-commit';
    await publish({github, context, core});
    assert.equal(calls.length, 0);
    head = 'source';
    await publish({github, context, core});
    assert.deepEqual((calls[0] as CreateTree).tree.map(file => file.path), ['docs/architecture/schema.mermaid']);
    assert.equal((calls[0] as CreateTree).base_tree, 'old-tree');
    assert.deepEqual((calls[1] as CreateCommit).parents, ['source']);
    assert.equal((calls[2] as UpdateRef).force, false);
    assert.equal((calls[2] as UpdateRef).ref, 'heads/feature');
    assert.equal((calls[3] as Comment).issue_number, 12);

    assert.match((calls[3] as Comment).body, /blob\/commit\/docs\/architecture/);
    calls.length = 0;
    fs.writeFileSync('diagram-changes.json', '[]');
    context.payload = {action: 'opened', pull_request: {
      number: 12, head: {ref: 'feature', sha: 'source'},
    }};
    await publish({github, context, core});
    assert.equal(calls.length, 1);
    assert.equal((calls[0] as Comment).issue_number, 12);
    assert.match((calls[0] as Comment).body, /blob\/source\/docs\/architecture/);
  } finally {
    process.chdir(cwd);
    fs.rmSync(directory, {recursive: true, force: true});
  }
});

test('diagram PNG is uploaded as a binary blob and embedded in one PR comment', async () => {
  const cwd = process.cwd();
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'schema-png-test-'));
  const png = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
  let tree: TreeEntry[] = [];
  const comments: Comment[] = [];
  const github: GitHub = {
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
      pulls: {list: async () => [{number: 12}], listFiles: async () => []},
      issues: {createComment: async args => comments.push(args)},
    },
    paginate: async (method, args) => {
      const response = await method(args);
      return Array.isArray(response) ? response : response.data;
    },
  };
  try {
    process.chdir(directory);
    fs.mkdirSync('docs/architecture', {recursive: true});
    fs.writeFileSync('docs/architecture/schema.png', png);
    fs.writeFileSync('docs/architecture/schema.mermaid', 'erDiagram\n');
    fs.writeFileSync('diagram-changes.json', '[]');
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

test('manifest parser accepts only diagram paths and rejects malformed imports', () => {
  assert.deepEqual(parseManifest('[]'), []);
  assert.deepEqual(parseManifest('["docs/architecture/schema.png","docs/architecture/schema.png"]'),
    ['docs/architecture/schema.png']);
  for (const source of ['{', '{}', 'null', '[1]', '["../secret"]',
    '["sqlite/rev.md"]', '["/tmp/schema.md"]']) {
    assert.throws(() => parseManifest(source), /Invalid diagram manifest/);
  }
});

test('trusted fork commenter handles changed, unchanged, stale and repeated runs', async () => {
  const workflow = fs.readFileSync(path.join(__dirname, '../../workflows/schema-comment.yml'), 'utf8');
  const script = workflow.split('          script: |\n')[1]
    .split('\n').map(line => line.slice(12)).join('\n');
  const execute = new Function('github', 'context', `return (async () => { ${script} })();`);
  const comments: Comment[] = [];
  const run = {id: 42, head_sha: 'source', head_branch: 'feature',
    head_repository: {full_name: 'contributor/repo', owner: {login: 'contributor'}}};
  const pull = {number: 12, head: {sha: 'source', ref: 'feature', repo: {full_name: 'contributor/repo'}}};
  let artifacts = [{name: 'schema-documentation', id: 99, expired: false}];
  let existing: Array<{user: {login: string}; body: string}> = [];
  const github = {
    rest: {
      actions: {listWorkflowRunArtifacts: async () => artifacts},
      pulls: {list: async () => [pull]},
      issues: {
        listComments: async () => existing,
        createComment: async (args: Comment) => {comments.push(args);},
      },
    },
    paginate: async (method: () => Promise<unknown>) => method(),
  };
  const context = {repo: {owner: 'org', repo: 'repo'}, payload: {workflow_run: run}};
  await execute(github, context);
  assert.equal(comments.length, 1);
  assert.equal(comments[0].issue_number, 12);
  assert.match(comments[0].body, /actions\/runs\/42\/artifacts\/99/);
  existing = [{user: {login: 'github-actions[bot]'}, body: comments[0].body}];
  await execute(github, context);
  assert.equal(comments.length, 1);
  existing = [];
  pull.head.sha = 'newer';
  await execute(github, context);
  assert.equal(comments.length, 1);
  pull.head.sha = 'source';
  pull.head.repo.full_name = 'different/repo';
  await execute(github, context);
  assert.equal(comments.length, 1);
  pull.head.repo.full_name = 'contributor/repo';
  artifacts[0].expired = true;
  await execute(github, context);
  assert.equal(comments.length, 1);
  artifacts = [];
  await execute(github, context);
  assert.equal(comments.length, 1);
  artifacts = [{name: 'schema-documentation', id: 99, expired: false}];
  run.head_repository.full_name = 'org/repo';
  await execute(github, context);
  assert.equal(comments.length, 1);
});
