import fs from 'node:fs';
import {commitFiles, isCurrent, target} from './commit-files.js';
import {parseManifest} from './schema-manifest.js';
import type {PublisherOptions} from './schema-types.js';

// v2/docs/schema/<service>/<revision>.md, one per migration revision.
const DOCUMENT = /^v2\/docs\/schema\/([a-z0-9_]+)\/[A-Za-z0-9_]+\.md$/;
const COMMENT_LIMIT = 58000;

/** One comment's body: each new revision's document, with its ERD linked absolutely. */
export function commentBody(service: string, documents: string[], blob: string): string {
  let body = `Schema updated for \`${service}\`: green = added, red = removed, amber = changed `
    + `from the previous revision. [Latest ERD](${blob}/v2/docs/schema/latest_${service}.svg)\n\n`;
  for (const path of documents) {
    const directory = path.slice(0, path.lastIndexOf('/'));
    // The documents link their SVG relatively, which only resolves when browsing the repository.
    const content = fs.readFileSync(path, 'utf8')
      .replace(/\]\(([A-Za-z0-9_]+\.svg)\)/g, (_, svg: string) => `](${blob}/${directory}/${svg}?raw=true)`);
    const section = `${content}\n[Source](${blob}/${path})\n\n`;
    // Keep each comment under GitHub's size limit, however large the schema grows.
    body += body.length + section.length <= COMMENT_LIMIT
      ? section : `[${path}](${blob}/${path}) exceeds the comment limit; see the document.\n\n`;
  }
  return body;
}

export default async function publish(options: PublisherOptions, manifest = 'service-schema-changes.json') {
  const {github, context} = options;
  const {owner, repo} = context.repo;
  let files = parseManifest(fs.readFileSync(manifest, 'utf8'), 'services');
  const needsCommit = files.length > 0;
  // A branch may already have generated documents before its PR is opened.
  if (!needsCommit && context.payload.action === 'opened' && context.payload.pull_request) {
    const changed = await github.paginate(github.rest.pulls.listFiles, {
      owner, repo, pull_number: context.payload.pull_request.number, per_page: 100,
    });
    files = changed.filter(file => file.status !== 'removed' && DOCUMENT.test(file.filename))
      .map(file => file.filename);
  }
  if (!files.length) return;
  if (!await isCurrent(options)) return;
  const {branch, sha} = target(context);
  const publishedSha = needsCommit ? await commitFiles(options, files, 'docs: update service schemas') : sha;
  const documents = new Map<string, string[]>();
  for (const file of [...files].sort()) {
    const service = DOCUMENT.exec(file)?.[1];
    if (service) documents.set(service, [...documents.get(service) ?? [], file]);
  }
  const pulls = await github.paginate(github.rest.pulls.list, {
    owner, repo, head: `${owner}:${branch}`, state: 'open', per_page: 100,
  });
  const blob = `https://github.com/${owner}/${repo}/blob/${publishedSha}`;
  for (const pull of pulls) {
    for (const [service, paths] of documents) {
      await github.rest.issues.createComment({owner, repo, issue_number: pull.number,
        body: commentBody(service, paths, blob)});
    }
  }
  return publishedSha;
}
