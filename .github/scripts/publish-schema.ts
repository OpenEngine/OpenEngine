import fs from 'node:fs';
import {parseManifest} from './schema-manifest.js';
import type {PublisherOptions, TreeEntry} from './schema-types.js';

export default async function publish({github, context, core}: PublisherOptions, kind: 'schema' | 'packages' = 'schema') {
  const packages = kind === 'packages';
  let files = packages
    ? parseManifest(fs.readFileSync('package-changes.json', 'utf8'), 'packages')
    : parseManifest(fs.readFileSync('schema-changes.json', 'utf8'), 'schema');
  if (!packages) files = files.map(file => `docs/schema/${file}`);
  if (!packages && fs.existsSync('diagram-changes.json')) {
    files.push(...parseManifest(fs.readFileSync('diagram-changes.json', 'utf8'), 'diagram'));
  }
  const needsCommit = files.length > 0;
  const {owner, repo} = context.repo;
  // A branch may already have generated documents before its PR is opened.
  if (!packages && !needsCommit && context.payload.action === 'opened' && context.payload.pull_request) {
    const changed = await github.paginate(github.rest.pulls.listFiles, {
      owner, repo, pull_number: context.payload.pull_request.number, per_page: 100,
    });
    files = changed.filter(file => file.status !== 'removed'
      && (/^docs\/schema\/[^/]+\/[^/]+\.md$/.test(file.filename)
        || /^docs\/architecture\/schema\.(mermaid|png)$/.test(file.filename)))
      .map(file => file.filename);
  }
  if (!files.length) return;
  const branch = context.payload.pull_request?.head.ref || context.ref.replace('refs/heads/', '');
  const sha = context.payload.pull_request?.head.sha || context.sha;
  const ref = await github.rest.git.getRef({owner, repo, ref: `heads/${branch}`});
  if (ref.data.object.sha !== sha) {
    core.notice('Branch advanced; the newer workflow run will publish its documentation.');
    return;
  }
  const contents = files.map(file => ({
    path: file,
    mode: '100644' as const, type: 'blob' as const,
    content: file.endsWith('.png') ? null : fs.readFileSync(file, 'utf8'),
  }));
  let publishedSha = sha;
  if (needsCommit) {
    const parent = await github.rest.git.getCommit({owner, repo, commit_sha: sha});
    const treeEntries: TreeEntry[] = [];
    for (const file of contents) {
      if (file.path.endsWith('.png')) {
        const blob = await github.rest.git.createBlob({owner, repo,
          content: fs.readFileSync(file.path).toString('base64'), encoding: 'base64'});
        treeEntries.push({path: file.path, mode: file.mode, type: 'blob', sha: blob.data.sha});
      } else {
        treeEntries.push(file);
      }
    }
    const tree = await github.rest.git.createTree({
      owner, repo, base_tree: parent.data.tree.sha, tree: treeEntries,
    });
    // Preserve the no-op guard: identical trees must not create bot commits.
    if (tree.data.sha === parent.data.tree.sha) {
      core.notice('Generated documentation already matches the branch; nothing to publish.');
      return sha;
    }
    const commit = await github.rest.git.createCommit({
      owner, repo, message: packages ? 'docs: update package architecture' : 'docs: update database schema',
      tree: tree.data.sha, parents: [sha],
    });
    // Non-fast-forward updates fail if another commit arrived during generation.
    await github.rest.git.updateRef({
      owner, repo, ref: `heads/${branch}`, sha: commit.data.sha, force: false,
    });
    publishedSha = commit.data.sha;
  }
  const pulls = await github.paginate(github.rest.pulls.list, {
    owner, repo, head: `${owner}:${branch}`, state: 'open', per_page: 100,
  });
  for (const pull of pulls) {
    if (packages) {
      const url = `https://github.com/${owner}/${repo}/blob/${publishedSha}/docs/architecture/packages`;
      await github.rest.issues.createComment({owner, repo, issue_number: pull.number,
        body: `Latest package architecture (arrows point to declared dependencies; dashed = optional).\n\n`
          + `![Package architecture](${url}.png?raw=true)\n\n[Mermaid source](${url}.mermaid)`});
      continue;
    }
    if (fs.existsSync('docs/architecture/schema.png')) {
      const url = `https://github.com/${owner}/${repo}/blob/${publishedSha}/docs/architecture/schema`;
      const body = `Latest schema ERD: green = added, red = removed, amber = changed. `
        + `Removed objects are comparison-only tombstones. Comparison uses the PR base `
        + `(or the preceding push when no PR is open).\n\n`
        + `![Schema changes](${url}.png?raw=true)\n\n[Mermaid source](${url}.mermaid)`;
      await github.rest.issues.createComment({owner, repo, issue_number: pull.number, body});
      continue;
    }
    for (const file of contents) {
      if (file.content === null) continue;
      const url = `https://github.com/${owner}/${repo}/blob/${publishedSha}/${file.path}`;
      const heading = `Schema updated: [${file.path}](${url})\n\n`;
      // Keep each comment under GitHub's size limit, including large schemas.
      const body = heading + (file.content.length <= 58000
        ? file.content
        : 'The full schema and highlighted changes exceed the comment limit; see the revision document above.');
      await github.rest.issues.createComment({owner, repo, issue_number: pull.number, body});
    }
  }
  return publishedSha;
};
