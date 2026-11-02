// jscpd 4.0.5 supplies the language lexers; exact token windows form clone groups.
const fs = require('node:fs');
const path = require('node:path');
const {createHash} = require('node:crypto');
const {tokenize, getFormatByFile} = require('@jscpd/tokenizer');
const config = require('./duplication.json');

function scan(root, options = config) {
  if (!Number.isInteger(options.minTokens) || options.minTokens < 1) throw new Error('minTokens must be a positive integer');
  const groups = new Map();
  function visit(directory) {
    for (const entry of fs.readdirSync(directory, {withFileTypes: true}).sort((a, b) => a.name.localeCompare(b.name))) {
      const absolute = path.join(directory, entry.name);
      const relative = path.relative(root, absolute).split(path.sep).join('/');
      if (options.exclude.includes(entry.name) || options.excludePaths.some(p => relative === p || relative.startsWith(p + '/'))) continue;
      if (entry.isDirectory()) { visit(absolute); continue; }
      if (!entry.isFile() || !options.extensions.includes(path.extname(entry.name))) continue;
      const format = getFormatByFile(entry.name);
      if (!format) throw new Error(`Unsupported source: ${relative}`);
      const source = fs.readFileSync(absolute, 'utf8');
      const tokens = tokenize(source, format).filter(t => !['comment', 'empty', 'new_line'].includes(t.type));
      for (let i = 0; i <= tokens.length - options.minTokens; i++) {
        const block = tokens.slice(i, i + options.minTokens);
        // Values, including identifier and literal spelling, are significant. Token
        // boundaries prevent concatenation collisions; lexical roles can vary by context.
        const id = createHash('sha256').update(JSON.stringify([format, block.map(t => t.value)])).digest('hex');
        const location = {file: relative, start: block[0].loc.start.line, end: block.at(-1).loc.end.line};
        if (!groups.has(id)) groups.set(id, {id, locations: [], context: source.slice(block[0].range[0], block.at(-1).range[1])});
        groups.get(id).locations.push(location);
      }
    }
  }
  visit(root);
  // Singleton windows cannot establish baseline debt or a qualifying clone.
  for (const [id, group] of groups) if (Object.keys(counts(group)).length < 2) groups.delete(id);
  return groups;
}

function counts(group) {
  const result = {};
  for (const {file} of group?.locations || []) result[file] = (result[file] || 0) + 1;
  return result;
}

function findings(current, base, exceptions = []) {
  for (const e of exceptions) {
    if (!/^[a-f0-9]{64}$/.test(e.id) || !e.rationale?.trim() || !e.files ||
        Object.keys(e.files).length < 3 || Object.entries(e.files).some(([p, n]) => /[*?]/.test(p) || !Number.isInteger(n) || n < 1)) {
      throw new Error('Exceptions require a content hash, exact file/count scope, and rationale');
    }
  }
  return [...current.values()].filter(group => {
    const now = counts(group), before = counts(base.get(group.id));
    if (Object.keys(now).length < 3 || !Object.keys(now).some(p => now[p] > (before[p] || 0))) return false;
    return !exceptions.some(e => e.id === group.id && Object.keys(now).every(p => now[p] <= (e.files[p] || 0)));
  });
}

if (require.main === module) {
  const [base, current = '.'] = process.argv.slice(2);
  if (!base || !fs.statSync(base).isDirectory()) throw new Error('Usage: npm run check -- /path/to/intended-base-tree [current-tree]');
  const result = findings(scan(path.resolve(current)), scan(path.resolve(base)), config.exceptions);
  console.log(JSON.stringify({detector: 'jscpd@4.0.5/token-windows-v1', minTokens: config.minTokens, findings: result}, null, 2));
  if (result.length) {
    console.error('Extract an appropriate shared abstraction or add a content- and file-scoped exception with rationale in tools/quality/duplication.json.');
    process.exitCode = 1;
  }
}
module.exports = {scan, findings, counts};
