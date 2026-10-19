import fs from 'node:fs';
import {parseManifest} from './schema-manifest.js';
import {commitFiles, isCurrent, target} from './commit-files.js';
import type {PublisherOptions} from './schema-types.js';

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
  if (!await isCurrent({github, context, core})) return;
  const {branch, sha} = target(context);
  const contents = files.map(file => ({
    path: file, content: file.endsWith('.png') ? null : fs.readFileSync(file, 'utf8'),
  }));
  const publishedSha = needsCommit
    ? await commitFiles({github, context, core}, files,
      packages ? 'docs: update package architecture' : 'docs: update database schema')
    : sha;
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
