// jscpd 4.0.5 supplies the language lexers; exact token windows form clone groups.
const fs = require('node:fs');
const path = require('node:path');
const {createHash} = require('node:crypto');
const {isUtf8} = require('node:buffer');
const {tokenize, getFormatByFile} = require('@jscpd/tokenizer');
const config = require('./duplication.json');

// Unknown languages still receive exact lexical matching without a lexer update.
function textTokens(source) {
  const tokens = [];
  let line = 1, offset = 0;
  for (const match of source.matchAll(/[\p{L}\p{N}_]+|[^\s]/gu)) {
    line += (source.slice(offset, match.index).match(/\n/g) || []).length;
    offset = match.index;
    tokens.push({value: match[0], range: [offset, offset + match[0].length],
      loc: {start: {line}, end: {line}}});
  }
  return tokens;
}

function scan(root, options = config) {
  if (!Number.isInteger(options.minTokens) || options.minTokens < 1) throw new Error('minTokens must be a positive integer');
  const index = new Map();
  const files = [];
  const sequences = [];
  const vocabulary = new Map();
  const values = [];
  const width = options.minTokens;
  let power = 1;
  for (let i = 1; i < width; i++) power = Math.imul(power, 31) >>> 0;
  function visit(directory) {
    for (const entry of fs.readdirSync(directory, {withFileTypes: true}).sort((a, b) => a.name.localeCompare(b.name))) {
      const absolute = path.join(directory, entry.name);
      const relative = path.relative(root, absolute).split(path.sep).join('/');
      if (options.exclude.includes(entry.name) || options.excludePaths.some(p => relative === p || relative.startsWith(p + '/'))) continue;
      if (entry.isDirectory()) { visit(absolute); continue; }
      if (!entry.isFile()) continue;
      const format = getFormatByFile(entry.name) || 'text';
      const fileIndex = files.push(relative) - 1;
      const bytes = fs.readFileSync(absolute);
      // Binary data is not code. Validate UTF-8 rather than matching replacement characters.
      if (bytes.includes(0) || !isUtf8(bytes)) continue;
      const source = bytes.toString('utf8');
      const tokens = format === 'text' ? textTokens(source) :
        tokenize(source, format).filter(t => !['comment', 'empty', 'new_line'].includes(t.type));
      const sequence = Uint32Array.from(tokens, token => {
        if (!vocabulary.has(token.value)) {
          vocabulary.set(token.value, values.length);
          values.push(token.value);
        }
        return vocabulary.get(token.value) + 1;
      });
      sequences[fileIndex] = sequence;
      let fingerprint = 0;
      for (let i = 0; i < sequence.length; i++) {
        if (i >= width) fingerprint = (fingerprint - Math.imul(sequence[i - width], power)) >>> 0;
        fingerprint = (Math.imul(fingerprint, 31) + sequence[i]) >>> 0;
        if (i + 1 < width) continue;
        const start = i + 1 - width;
        const key = `${format}:${fingerprint}`;
        const location = [fileIndex, tokens[start].loc.start.line, tokens[i].loc.end.line,
          tokens[start].range[0], tokens[i].range[1], start];
        const previous = index.get(key);
        if (previous) previous.push(...location);
        else index.set(key, location);
      }
    }
  }
  visit(root);
  const groups = new Map();
  const contexts = new Map();
  for (const [key, records] of index) {
    let crossFile = false;
    for (let i = 6; i < records.length; i += 6) {
      if (records[i] !== records[0]) { crossFile = true; break; }
    }
    if (!crossFile) continue;
    // Fingerprints select candidates only. Compare every token to prevent hash
    // collisions from joining unrelated blocks, then hash each exact group once.
    const partitions = [];
    for (let i = 0; i < records.length; i += 6) {
      const sequence = sequences[records[i]], start = records[i + 5];
      let partition = partitions.find(p => {
        const representative = sequences[records[p[0]]], offset = records[p[0] + 5];
        for (let j = 0; j < width; j++) if (sequence[start + j] !== representative[offset + j]) return false;
        return true;
      });
      if (!partition) partitions.push(partition = []);
      partition.push(i);
    }
    for (const partition of partitions) {
      const first = partition[0], fileIndex = records[first];
      if (!partition.some(i => records[i] !== fileIndex)) continue;
      const start = records[first + 5];
      const block = Array.from(sequences[fileIndex].subarray(start, start + width), id => values[id - 1]);
      const format = key.slice(0, key.lastIndexOf(':'));
      const id = createHash('sha256').update(JSON.stringify([format, block])).digest('hex');
      const locations = partition.map(i => ({file: files[records[i]], start: records[i + 1], end: records[i + 2]}));
      const group = {id, locations};
      groups.set(id, group);
      const file = files[fileIndex];
      if (!contexts.has(file)) contexts.set(file, []);
      contexts.get(file).push({group, start: records[first + 3], end: records[first + 4]});
    }
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
  // npm changes cwd to the package directory; INIT_CWD preserves the caller's
  // directory for both the check script and the scanner spawned by the gate.
  const invocationDirectory = process.env.INIT_CWD || process.cwd();
  const [baseArgument, currentArgument = '.'] = process.argv.slice(2);
  const base = baseArgument && path.resolve(invocationDirectory, baseArgument);
  const current = path.resolve(invocationDirectory, currentArgument);
  if (!base || !fs.statSync(base).isDirectory()) throw new Error('Usage: npm run check -- /path/to/intended-base-tree [current-tree]');
  const result = findings(scan(path.resolve(current)), scan(path.resolve(base)), config.exceptions);
  console.log(JSON.stringify({detector: 'jscpd@4.0.5/token-windows-v1', minTokens: config.minTokens, findings: result}, null, 2));
  if (result.length) {
    console.error('Extract an appropriate shared abstraction or add a content- and file-scoped exception with rationale in tools/quality/duplication.json.');
    process.exitCode = 1;
  }
}
module.exports = {scan, findings, counts};
