import fs from 'node:fs';
import type {Context, PublisherOptions, TreeEntry} from './schema-types.js';

/** The branch and commit a workflow run documents. */
export function target(context: Context) {
  return {
    branch: context.payload.pull_request?.head.ref || context.ref.replace('refs/heads/', ''),
    sha: context.payload.pull_request?.head.sha || context.sha,
  };
}

/** Whether the branch still points at the run's commit; a newer run publishes otherwise. */
export async function isCurrent({github, context, core}: PublisherOptions): Promise<boolean> {
  const {owner, repo} = context.repo;
  const {branch, sha} = target(context);
  const ref = await github.rest.git.getRef({owner, repo, ref: `heads/${branch}`});
  if (ref.data.object.sha === sha) return true;
  core.notice('Branch advanced; the newer workflow run will publish its documentation.');
  return false;
}

/** Commit generated files from the working tree onto the run's branch and return the new commit. */
export async function commitFiles({github, context}: PublisherOptions, files: string[], message: string) {
  const {owner, repo} = context.repo;
  const {branch, sha} = target(context);
  const parent = await github.rest.git.getCommit({owner, repo, commit_sha: sha});
  const tree: TreeEntry[] = [];
  for (const path of files) {
    if (path.endsWith('.png')) {
      const blob = await github.rest.git.createBlob({owner, repo,
        content: fs.readFileSync(path).toString('base64'), encoding: 'base64'});
      tree.push({path, mode: '100644', type: 'blob', sha: blob.data.sha});
    } else {
      tree.push({path, mode: '100644', type: 'blob', content: fs.readFileSync(path, 'utf8')});
    }
  }
  const created = await github.rest.git.createTree({owner, repo, base_tree: parent.data.tree.sha, tree});
  const commit = await github.rest.git.createCommit({owner, repo, message, tree: created.data.sha, parents: [sha]});
  // Non-fast-forward updates fail if another commit arrived during generation.
  await github.rest.git.updateRef({owner, repo, ref: `heads/${branch}`, sha: commit.data.sha, force: false});
  return commit.data.sha;
}
