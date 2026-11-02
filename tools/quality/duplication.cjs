// jscpd 4.0.5 supplies the language lexers; exact token windows form clone groups.
const fs = require('node:fs');
const path = require('node:path');
const {createHash} = require('node:crypto');
const {tokenize, getFormatByFile} = require('@jscpd/tokenizer');
const config = require('./duplication.json');

function scan(root, options = config) {
  if (!Number.isInteger(options.minTokens) || options.minTokens < 1) throw new Error('minTokens must be a positive integer');
  const index = new Map();
  const files = [];
  function visit(directory) {
    for (const entry of fs.readdirSync(directory, {withFileTypes: true}).sort((a, b) => a.name.localeCompare(b.name))) {
      const absolute = path.join(directory, entry.name);
      const relative = path.relative(root, absolute).split(path.sep).join('/');
      if (options.exclude.includes(entry.name) || options.excludePaths.some(p => relative === p || relative.startsWith(p + '/'))) continue;
      if (entry.isDirectory()) { visit(absolute); continue; }
      if (!entry.isFile() || !options.extensions.includes(path.extname(entry.name))) continue;
      const format = getFormatByFile(entry.name);
      if (!format) throw new Error(`Unsupported source: ${relative}`);
      const fileIndex = files.push(relative) - 1;
      const source = fs.readFileSync(absolute, 'utf8');
      const tokens = tokenize(source, format).filter(t => !['comment', 'empty', 'new_line'].includes(t.type));
      for (let i = 0; i <= tokens.length - options.minTokens; i++) {
        const block = tokens.slice(i, i + options.minTokens);
        // Values, including identifier and literal spelling, are significant. Token
        // boundaries prevent concatenation collisions; lexical roles can vary by context.
        const id = createHash('sha256').update(JSON.stringify([format, block.map(t => t.value)])).digest('hex');
        // Retain only flat numeric locations until cross-file repetition is known.
        const location = [fileIndex, block[0].loc.start.line, block.at(-1).loc.end.line,
          block[0].range[0], block.at(-1).range[1]];
        const previous = index.get(id);
        if (previous) previous.push(...location);
        else index.set(id, location);
      }
    }
  }
  visit(root);
  const groups = new Map();
  const contexts = new Map();
  for (const [id, records] of index) {
    let repeated = false;
    for (let i = 5; i < records.length; i += 5) {
      if (records[i] !== records[0]) { repeated = true; break; }
    }
    if (!repeated) continue;
    const locations = [];
    for (let i = 0; i < records.length; i += 5) {
      locations.push({file: files[records[i]], start: records[i + 1], end: records[i + 2]});
    }
    const group = {id, locations};
    groups.set(id, group);
    const file = files[records[0]];
    if (!contexts.has(file)) contexts.set(file, []);
    contexts.get(file).push({group, start: records[3], end: records[4]});
  }
  index.clear();
  // Read each representative source once, only for retained repeated groups.
  for (const [file, entries] of contexts) {
    const source = fs.readFileSync(path.join(root, file), 'utf8');
    for (const {group, start, end} of entries) group.context = source.slice(start, end);
  }
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
    // Content identity matches relocated copies; splitting into more files still
    // expands a group even when its total number of copies stays the same.
    const total = counts => Object.values(counts).reduce((sum, n) => sum + n, 0);
    if (Object.keys(now).length < 3 ||
        (Object.keys(now).length <= Object.keys(before).length && total(now) <= total(before))) return false;
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
