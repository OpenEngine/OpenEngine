import assert from 'node:assert/strict';
import fs from 'node:fs';
import {test} from 'node:test';

// Exercise the actual trusted workflow script, without checking out PR code.
const workflow = fs.readFileSync(new URL('../workflows/package-comment.yml', import.meta.url), 'utf8');
const source = workflow.split('          script: |\n')[1]
  .split('\n').map(line => line.replace(/^            /, '')).join('\n');
const execute = new (Object.getPrototypeOf(async function () {}).constructor)('github', 'context', source);

async function run({sameRepo = false, sha = 'head', repository = 2, branch = 'feature',
                    artifacts = [{id: 42, name: 'package-architecture', expired: false}],
                    comments = []} = {}) {
  const posted = [];
  const github = {
    rest: {
      actions: {listWorkflowRunArtifacts: async args => {
        assert.equal(args.run_id, 123);
        return artifacts;
      }},
      pulls: {list: async () => [{number: 7, head: {sha, ref: branch, repo: {id: repository}}}]},
      issues: {listComments: async () => comments, createComment: async args => posted.push(args)},
    },
    paginate: (method, args) => method(args),
  };
  await execute(github, {repo: {owner: 'org', repo: 'repo'}, payload: {
    repository: {id: 1}, workflow_run: {id: 123, head_sha: 'head', head_branch: 'feature',
      head_repository: {id: sameRepo ? 1 : 2}, pull_requests: []},
  }});
  return posted;
}

test('fork run links PNG/Mermaid artifact even with no associated PRs in event', async () => {
  const posted = await run();
  assert.equal(posted.length, 1);
  assert.equal(posted[0].issue_number, 7);
  assert.match(posted[0].body, /https:\/\/github.com\/org\/repo\/actions\/runs\/123\/artifacts\/42/);
  assert.match(posted[0].body, /packages.png and packages.mermaid/);
});

test('same-repo, unchanged, expired, stale and unrelated runs do not comment', async () => {
  for (const options of [{sameRepo: true}, {artifacts: []},
    {artifacts: [{id: 42, name: 'package-architecture', expired: true}]},
    {sha: 'new-head'}, {repository: 3}, {branch: 'other'}]) {
    assert.deepEqual(await run(options), []);
  }
});

test('reruns do not duplicate bot comments, and user comments cannot suppress publication', async () => {
  const [comment] = await run();
  assert.deepEqual(await run({comments: [{body: comment.body, user: {login: 'github-actions[bot]'}}]}), []);
  assert.equal((await run({comments: [{body: comment.body, user: {login: 'contributor'}}]})).length, 1);
});
