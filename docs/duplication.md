# Duplication quality gate

Run the local gate from the repository root before submitting a change:

```sh
npm --prefix tools/quality ci --ignore-scripts
npm --prefix tools/quality run --silent gate -- /absolute/path/to/base-checkout . > /tmp/duplication-report.json
```

Relative tree paths (including the default current tree, `.`) resolve from the
directory where npm was invoked. Direct Node invocations use their working
directory when `INIT_CWD` is unset.

The base checkout must be a clean tree of the intended PR base revision, not
an earlier working copy or the PR head. Export that revision with the repository's
Git tooling, or use a separate checkout. The gate never fetches or guesses a base.
CI checks out the PR head and exact event base SHA separately (the previous SHA
on main pushes) and uploads the JSON report even when findings fail the gate.
Missing or unreadable base trees fail closed. Both trees are scanned in full,
even if only one source file changed; no LLM chooses the files to inspect.

The detector uses pinned jscpd 4.0.5 and its pinned 4.0.1 language tokenizer,
with a content-window grouping policy in `tools/quality/duplication.cjs`.
`duplication.json` configures the source extensions, explicit directory/path
exclusions, and minimum size (initially 100 tokens). All Python and JavaScript /
TypeScript sources under the repository root are included, including tests,
subject to the explicit dependency, generated-code, build, fixture, and vendor
exclusions. Symlinks are not followed. Other languages are outside this initial
policy; extending the source extensions requires a supported jscpd lexer.

Each group shares exactly the same sequence of at least 100 consecutive lexical
token values, in the same language. Whitespace and comments are ignored;
identifier and literal spelling remain significant. Renamed variables and semantic
duplication are not detected. Syntax similarity does not establish shared
responsibility: review the context before choosing an abstraction. The JSON
contains a stable SHA-256 content identifier, shared source context, and **all**
matching file/line locations. Long clones can produce overlapping windows;
each is an independently actionable substantial shared block. This intentionally
avoids treating unrelated pairwise matches as one three-file clone.

Two distinct files are permitted. A group fails only when it occurs in at least
three distinct files and either the total occurrence count or distinct file count
increases relative to the base. Thus a third or fourth file, or an additional copy
within an already qualifying group, fails. Existing unchanged debt, line shifts,
and content-preserving relocations pass. Changed content gets a new identity: a change shared by three files fails; changing only
one copy does not fail if the remaining matching content has no new occurrences.
Renames are matched by content rather than path. Removing copies passes.

Resolve a finding by extracting an appropriate abstraction, or add an explicit
exception to `tools/quality/duplication.json` for independently owned code:

```json
{"id": "<64-character content hash from report>",
 "files": {"a.py": 1, "b.py": 1, "c.py": 1},
 "rationale": "Explain why these exact copies have separate responsibilities."}
```

Use the full hash from each reported window being excepted. File scopes are
literal repository-relative paths and maximum occurrence counts; globs and empty
rationales are rejected. Another file, more occurrences, or changed shared tokens
cannot inherit the exception. Exceptions and changes to detector settings are
reviewable code changes; never suppress a finding with a blanket exclusion.
