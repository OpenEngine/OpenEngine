# Whole-repository duplication gate

Run from the repository root, with a **separate checkout of the intended base
revision** (for a PR, its base SHA, not the previous local commit):

```sh
uv run --project tools/quality --locked python tools/quality/gate.py --baseline-root /path/to/base > duplication.json
```

This command tests the detector, then runs the local gate. `duplication.yml` runs it for
every PR, checking out the exact head and base SHAs into sibling directories.
It uploads JSON even when the gate fails. No changed-file filter or LLM search
participates: both entire configured source trees are scanned on every run.
Exit codes: 0 passes, 1 reports new duplication, 2 reports scan/policy errors.
The baseline must exist and differ from the current tree.

## Matching contract

The pinned detector is `engine-duplication-gate` 2.0.0 (`tree-clones-v2`), with
Tree-sitter 0.25.2 and language-pack 0.10.0. The isolated `uv.lock` pins grammar
artifacts. Both revisions use the head detection policy. CODEOWNERS requires
maintainer review of `tools/quality/duplicates.toml`; repository branch protection
must enforce code-owner review to prevent self-approved policy loosening.

Three channels participate, with `kind` values `exact`, `type2` and `type3`:

- Exact: ordered syntax-subtree hashes preserve identifiers, literals, operators,
  keywords and structure. Comments and Python backslash continuations outside
  literals are ignored. Strings/templates are atomic, preserving their bytes.
- Type-2: a second channel in `fingerprints()` blinds identifiers to ID, numeric
  literals to NUM and strings/templates to STR. Operators, keywords and structure
  stay. Hashes are grouped directly; there is no similarity chaining.
- Type-3: functions, methods and classes of at least `unit_min_tokens` (100) use
  sets of blinded inner-subtree hashes with at least five tokens. Inner function
  and class wrappers are omitted from shingles. An inverted index generates
  candidate pairs sharing shingles within a grammar. Exact set Jaccard verifies
  each candidate at `similarity_threshold` (0.8). No sampling or MinHash is used.
  Units sorted by path, byte offset and exact hash join the first complete-link
  cluster whose every member meets the threshold. This conservative greedy
  partition is deterministic; it can miss overlapping cliques. A-B and B-C never
  imply A-C. Only clusters spanning at least three distinct files are reported.

`min_tokens` defaults to 100 for direct matches. Long strings count as one token.
Module/root wrappers are excluded: removing an unrelated module setting must not
create a new clone of an unchanged function. A group is suppressed when every
location is contained in a larger reported group with the same file spread.
Equal spans prefer exact, then Type-2, then Type-3. Reports therefore show a cloned
function once; raw scan groups retain channel information for inspection.

Python, JavaScript (.js/.mjs), TypeScript, TSX, Bash, Rust and Go extensions match
case-insensitively. Unsupported files are listed in JSON; embedded code in HTML,
CSS, Markdown and other unsupported formats is not scanned. Grammar smoke tests
cover every supported language. Grammar acceptance is not compiler validation.
Syntax similarity suggests candidates for abstraction, not semantic equivalence.
Blinding can match unrelated responsibilities; shingles discard repetition counts
and statement order, and arbitrary repeated runs without shared subtrees can be
missed. Review the differences before extracting an abstraction.

Tests are exempt under DAMP, in both direct and fuzzy layers. `test_patterns` in
`duplicates.toml` match path components: `tests`, `__tests__`, `test_*.py`,
`*_test.py`, `*.test.ts`, `*.test.tsx`, `*.test.js`, `*.spec.*`, and `conftest.py`.
JSON `excluded_tests` lists test files omitted from scanning. Dependencies,
generated code, build output and detector fixtures remain explicitly excluded.
Symlinks are not followed. There is no large-file skip. Parse failures and read
errors fail closed; `files` counts only successfully parsed files. The former TSX
test grammar waiver is unnecessary because tests no longer participate.

Two similar files remain allowed. Exact and Type-2 groups fail only when they
span three files and distinct-file count or occurrence count increases versus
base. Type-3 findings require a qualifying group containing an exact unit identity
whose count increased versus base (including a previously unseen exact hash).
This detects new third/fourth copies as well as edits joining existing near-clones.
Unchanged debt, content-preserving moves, renames and line shifts pass. Replacing a
removed copy with an identical copy elsewhere is treated as relocation. A changed
literal in one member of a three-file fuzzy group can now require review.

Diagnostics include all locations. Fuzzy members include similarity to a named
reference, differing shingle hashes and bounded statement excerpts for those
shingles. Scores reference the least similar peer so differences remain visible.
Source context is bounded to 1,200 bytes per finding and read by byte range.
JSON serialization is sorted and contains no timings or nondeterministic fields;
identical inputs produce identical bytes. Use `benchmark.py` for timing and RSS.

Resolve a finding by extracting an appropriate abstraction or adding a scoped
exception with rationale:

```toml
[[exceptions]]
fingerprint = "<key from diagnostic>"
files = { "a.py" = 1, "b.py" = 1, "c.py" = 1 }
rationale = "Explain why these copies should remain independently owned."
```

Replace `exceptions = []` with these entries. Direct keys use channel fingerprints;
Type-3 exception keys hash the sorted list of unit blinded hashes and exact paths,
including multiplicities. The JSON members expose those identities for review.
Exact file/count scope, a nonempty rationale and no wildcard paths are required.
Growth, moves or structural changes invalidate the exception. Blinded exceptions
intentionally survive identifier/literal changes. Stale exceptions fail closed.
Reviewed parse waivers from base remain available only to its baseline scan.

## Type-2/Type-3 calibration

Measured against PR base `39ea9a1f2c37b1a35c8263f76d443ba83222e260`, macOS
arm64, warm caches. Each row is a fresh process scanning all 503 non-test source
files (3,922,573 bytes), then grouping all layers. Counts are qualifying groups
after nested suppression within direct and fuzzy layers; exact wins equal direct
spans. There are 1,658 eligible units and no parse errors.

| Threshold | Exact | Type-2 | Type-3 | Scan + grouping seconds | Peak RSS MiB |
| --- | --- | --- | --- | --- | --- |
| 0.7 | 0 | 0 | 1 | 5.28 | 83.55 |
| 0.8 | 0 | 0 | 1 | 5.23 | 82.56 |
| 0.9 | 0 | 0 | 1 | 5.08 | 82.80 |

Default 0.8 retains the added-branch regression while producing only one existing
group, so base debt is tolerable and unchanged debt is nonblocking. That group
contains functions in runtime/terminal_mcp.py, single_tool_mcp/__init__.py and
slack_concierge/slack_egress.py, with minimum pair similarity 0.9375. These are
measurements, not evidence of semantic equivalence or enforced CI budgets.

Reproduce each threshold with (substitute 0.7 and 0.9):

```sh
uv run --project tools/quality --locked python tools/quality/benchmark.py --root /path/to/base --threshold 0.8
```

Three generated ~1 MiB Python files (30,000 assignments each) took 2.66 seconds
and 380.34 MiB peak RSS with both blinded and exact trees. One direct and one
fuzzy group collapse to one finding. No file-size skip applies. Candidate work
can approach quadratic for a repository where every unit shares a shingle;
there is no unconditional all-pairs similarity pass. Large nested classes and
many candidate matches still increase memory and runtime. Context excerpts remain
bounded even for deeply nested expressions.

Descendant collection has a separate scaling limit: for each eligible function,
method or class, it scans the file's inner-subtree list to collect shingles and
contexts. This costs O(U × N), where U is the number of eligible units and N is
the number of inner subtrees in that file. A single file containing hundreds of
functions can therefore cause quadratic work as it grows, even when candidate
similarity checks are inexpensive. Current repository scans take about five
seconds, and maintainers consider this limit non-blocking for this PR. Keeping
files and functions small is tracked in
[#1324](https://github.com/OpenEngine/OpenEngine/issues/1324), covering function
size and complexity limits coordinated with
[#779](https://github.com/OpenEngine/OpenEngine/issues/779). Optimizing descendant
collection is deferred; this PR does not introduce size limits.

## Evaluation and selection

Evaluated before selecting the gate implementation, on macOS arm64, CPython
3.14.7, base `39ea9a1f`, with warm dependency caches. These are observations,
not CI performance budgets. Reproduce prototype measurements with:

```sh
uv run --project tools/quality --locked python tools/quality/benchmark.py --root /path/to/base
uv run --project tools/quality --locked python tools/quality/benchmark.py --large
```

| Approach | Observed cost and behavior | Decision |
| --- | --- | --- |
| Exact Tree-sitter prototype, selected | One scan: 764 supported files, 7,751,082 bytes, 1.91 s, 47.5 MiB peak RSS. Two-revision gate: ~3.6 s. Three generated ~1 MiB Python files: 1.18 s, 240.7 MiB RSS, three shared subtree groups. | Deterministic exact groups, bounded per-node hashing work, small dependencies, explicit grammar failures. |
| [treepeat](https://github.com/dsummersl/treepeat) 0.11.0, executed with `--ruleset none --min-lines 10 --format sarif` | Whole clean base: 6.78 s, 274.3 MiB child peak RSS, 12 SARIF results. Different coverage (also documentation/embedded languages), so counts are not directly comparable. It warns on the same TSX parse error. Installed source uses MinHash/LSH plus union-find in `pipeline/lsh_stage.py`. Default/loose normalization deliberately discards literal/name information. | Useful exploratory tool and SARIF output, but its grouping/normalization cannot establish this policy's exact common-block contract without another exact validation layer. |
| [CCDetect-lsp](https://github.com/jakobkhansen/CCDetect-lsp), documentation evaluation | Tree-sitter fragment queries, token thresholds and optional identifier/literal blinding; incremental editor server. Build requires Java/Gradle and compiled native grammar submodules. No standalone repository/baseline policy in its documented interface. Not installed or benchmarked. | Better suited to interactive discovery; adds an LSP/native build and still needs this gate's policy layer. |
| Abandoned [#1315](https://github.com/OpenEngine/OpenEngine/pull/1315), executed from its diff with its lockfile | jscpd 4.0.5/tokenizer 4.0.1, exact rolling token windows, not AST matching. Its complete base-vs-base scan: 5.15 s, 1,035 MiB child peak RSS, zero new findings. Broader text coverage than the prototype; includes a generic text fallback. | Fixed-size windows catch arbitrary repeated statement runs, but can split syntax and produce many overlapping windows; token storage/window indexing costs more on this input. Its content/count baseline policy is retained conceptually. |

Runtime/memory measurements exclude dependency installation; child RSS was
measured with `resource.getrusage(RUSAGE_CHILDREN)`, prototype RSS with
`RUSAGE_SELF`. Treepeat invocation excluded `.git`, static site output and the
generated graph report. #1315 used its own configured exclusions. The prototype
large-file benchmark uses 30,000 assignment statements per file, so it exercises
large syntax trees rather than only large strings. Memory scales with syntax
tree size, candidate count and retained matching locations. The v2 descendant
collection and candidate scaling limits are described above. Parse stacks are
iterative to avoid Python recursion limits.

Treepeat's exact ruleset also ran on four small Python fixture trees: it emitted
one three-region group for identical functions, one for formatting/comment-only
differences, none when literal values differed, and two separate two-region
groups for an unrelated pairwise chain. Those examples work; the source-level
union-find behavior remains the reason to require exact group validation rather
than assume every output cluster shares one substantial block.

The regression suite validates three-file matches, disjoint pairwise chains,
small boilerplate, exact literal/name preservation, formatting/comments,
all supported grammars, large files, exclusions, baseline transitions, relocation,
content changes, exception growth, parse failures and machine-readable CLI output.
The original evaluation below predates Type-2/Type-3 support; the new calibration
applies the DAMP exclusions and deterministic similarity policy described above.
