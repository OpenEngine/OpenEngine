import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {test} from 'node:test';
import publish from './publish-service-schemas.js';
import {parseManifest} from './schema-manifest.js';
import type {Comment, Context, CreateCommit, CreateTree, GitHub, UpdateRef} from './schema-types.js';

function fake(head: () => string, files: Array<{filename: string; status: string}> = []) {
  const calls: Array<CreateTree | CreateCommit | UpdateRef | Comment> = [];
  const github: GitHub = {
    rest: {
      git: {
        createBlob: async () => {throw new Error('SVGs and documents are text');},
        getRef: async () => ({data: {object: {sha: head()}}}),
        getCommit: async () => ({data: {tree: {sha: 'old-tree'}}}),
        createTree: async args => { calls.push(args); return {data: {sha: 'tree'}}; },
        createCommit: async args => { calls.push(args); return {data: {sha: 'commit'}}; },
        updateRef: async args => { calls.push(args); },
      },
      pulls: {list: async () => [{number: 7}], listFiles: async () => files},
      issues: {createComment: async args => { calls.push(args); }},
    },
    paginate: async (method, args) => {
      const response = await method(args);
      return Array.isArray(response) ? response : response.data;
    },
  };
  return {github, calls};
}

const context = (): Context => ({repo: {owner: 'org', repo: 'repo'}, payload: {}, ref: 'refs/heads/feature', sha: 'source'});
const core = {notice: () => {}};

function workspace(run: () => Promise<void>) {
  return async () => {
    const cwd = process.cwd();
    const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'service-schema-test-'));
    try {
      process.chdir(directory);
      for (const [service, revision] of [['cxn', 'aaa111'], ['vcp', 'bbb222']]) {
        fs.mkdirSync(`v2/docs/schema/${service}`, {recursive: true});
        fs.writeFileSync(`v2/docs/schema/${service}/${revision}.md`,
          `# ${service}: ${revision}\n\n![${service}](${revision}.svg)\n\n\`\`\`sql\nCREATE TABLE t;\n\`\`\`\n`);
        fs.writeFileSync(`v2/docs/schema/${service}/${revision}.svg`, '<svg/>');
        fs.writeFileSync(`v2/docs/schema/latest_${service}.svg`, '<svg/>');
      }
      await run();
    } finally {
      process.chdir(cwd);
      fs.rmSync(directory, {recursive: true, force: true});
    }
  };
}

test('commits the generated documents and comments once per changed service', workspace(async () => {
  const {github, calls} = fake(() => 'source');
  fs.writeFileSync('changes.json', JSON.stringify([
    'v2/docs/schema/cxn/aaa111.md', 'v2/docs/schema/cxn/aaa111.svg', 'v2/docs/schema/latest_cxn.svg',
    'v2/docs/schema/vcp/bbb222.md', 'v2/docs/schema/vcp/bbb222.svg',
  ]));

  assert.equal(await publish({github, context: context(), core}, 'changes.json'), 'commit');

  const tree = (calls[0] as CreateTree).tree;
  assert.equal(tree.length, 5);
  assert.equal(tree.find(entry => entry.path.endsWith('.svg'))?.content, '<svg/>');
  assert.equal((calls[1] as CreateCommit).message, 'docs: update service schemas');
  assert.equal((calls[2] as UpdateRef).force, false);
  const comments = calls.slice(3) as Comment[];
  assert.equal(comments.length, 2);
  assert.match(comments[0].body, /Schema updated for `cxn`/);
  assert.match(comments[0].body, /blob\/commit\/v2\/docs\/schema\/latest_cxn\.svg/);
  assert.ok(comments[0].body.includes('](https://github.com/org/repo/blob/commit/v2/docs/schema/cxn/aaa111.svg?raw=true)'));
  assert.match(comments[0].body, /CREATE TABLE t;/);
  assert.match(comments[1].body, /Schema updated for `vcp`/);
}));

test('skips unchanged and stale runs', workspace(async () => {
  let head = 'source';
  const {github, calls} = fake(() => head);
  fs.writeFileSync('changes.json', '[]');
  await publish({github, context: context(), core}, 'changes.json');
  assert.equal(calls.length, 0);

  fs.writeFileSync('changes.json', '["v2/docs/schema/cxn/aaa111.md"]');
  head = 'newer';
  await publish({github, context: context(), core}, 'changes.json');
  assert.equal(calls.length, 0);
}));

test('comments on a newly opened PR whose documents were generated earlier', workspace(async () => {
  const {github, calls} = fake(() => 'source', [
    {filename: 'v2/docs/schema/vcp/bbb222.md', status: 'added'},
    {filename: 'v2/docs/schema/latest_vcp.svg', status: 'added'},
    {filename: 'v2/docs/schema/cxn/old.md', status: 'removed'},
  ]);
  fs.writeFileSync('changes.json', '[]');
  const opened = {...context(), payload: {action: 'opened', pull_request: {number: 7, head: {ref: 'feature', sha: 'source'}}}};

  assert.equal(await publish({github, context: opened, core}, 'changes.json'), 'source');

  assert.equal(calls.length, 1);
  assert.match((calls[0] as Comment).body, /Schema updated for `vcp`/);
  assert.match((calls[0] as Comment).body, /blob\/source\//);
}));

test('keeps a comment under the size limit', workspace(async () => {
  const {github, calls} = fake(() => 'source');
  fs.writeFileSync('v2/docs/schema/cxn/aaa111.md', 'x'.repeat(60000));
  fs.writeFileSync('changes.json', '["v2/docs/schema/cxn/aaa111.md"]');

  await publish({github, context: context(), core}, 'changes.json');

  const comment = calls[3] as Comment;
  assert.ok(comment.body.length < 60000);
  assert.match(comment.body, /exceeds the comment limit/);
}));

test('services manifest accepts only generated v2 schema documents', () => {
  assert.deepEqual(parseManifest('["v2/docs/schema/latest_cxn.svg","v2/docs/schema/ge/b552e26457cd.mermaid"]', 'services'),
    ['v2/docs/schema/latest_cxn.svg', 'v2/docs/schema/ge/b552e26457cd.mermaid']);
  for (const source of ['["v2/docs/schema/../../secret.md"]', '["docs/schema/sqlite/rev.md"]',
    '["v2/docs/schema/cxn/rev.png"]', '["v2/docs/schema/cxn/nested/rev.md"]']) {
    assert.throws(() => parseManifest(source, 'services'), /Invalid services manifest/);
  }
});
