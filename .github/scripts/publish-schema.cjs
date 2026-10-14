const fs = require('node:fs');

module.exports = async ({github, context, core}) => {
  let files = JSON.parse(fs.readFileSync('schema-changes.json', 'utf8'));
  const needsCommit = files.length > 0;
  const {owner, repo} = context.repo;
  // A branch may already have generated documents before its PR is opened.
  if (!needsCommit && context.payload.action === 'opened') {
    const changed = await github.paginate(github.rest.pulls.listFiles, {
      owner, repo, pull_number: context.payload.pull_request.number, per_page: 100,
    });
    files = changed.filter(file => file.status !== 'removed'
      && /^docs\/schema\/[^/]+\/[^/]+\.md$/.test(file.filename))
      .map(file => file.filename.replace('docs/schema/', ''));
  }
  if (!files.length) return;
  const branch = context.payload.pull_request?.head.ref || context.ref.replace('refs/heads/', '');
  const sha = context.payload.pull_request?.head.sha || context.sha;
  const ref = await github.rest.git.getRef({owner, repo, ref: `heads/${branch}`});
  if (ref.data.object.sha !== sha) {
    core.notice('Branch advanced; the newer workflow run will publish its schema.');
    return;
  }
  const contents = files.map(file => ({
    path: `docs/schema/${file}`,
    mode: '100644', type: 'blob',
    content: fs.readFileSync(`docs/schema/${file}`, 'utf8'),
  }));
  let publishedSha = sha;
  if (needsCommit) {
    const parent = await github.rest.git.getCommit({owner, repo, commit_sha: sha});
    const tree = await github.rest.git.createTree({
      owner, repo, base_tree: parent.data.tree.sha, tree: contents,
    });
    const commit = await github.rest.git.createCommit({
      owner, repo, message: 'docs: update database schema',
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
    for (const file of contents) {
      const url = `https://github.com/${owner}/${repo}/blob/${publishedSha}/${file.path}`;
      const heading = `Schema updated: [${file.path}](${url})\n\n`;
      // Keep each comment under GitHub's size limit, including large schemas.
      const body = heading + (file.content.length <= 58000
        ? file.content
        : 'The full schema and highlighted changes exceed the comment limit; see the revision document above.');
      await github.rest.issues.createComment({owner, repo, issue_number: pull.number, body});
    }
  }
};
