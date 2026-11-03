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

The pinned detector is `engine-duplication-gate` 1.0.0 (`exact-tree-v1`), with
Tree-sitter 0.25.2 and language-pack 0.10.0. Its isolated `uv.lock` pins grammar
dependencies and artifact hashes. Change the detector version when changing
fingerprint semantics, and re-evaluate baseline behavior on upgrades.

Every complete syntax subtree with at least `min_tokens` leaf tokens (default
100) is a candidate. Ordered child hashes, node kinds and literal/identifier
bytes define identity; SHA-256 hashes are grouped **directly**, never by
transitive pairwise similarity. Comments and formatting outside literals are
ignored. String/template subtrees are atomic and preserve their entire bytes,
including whitespace, escaping and interpolation spelling. A long string alone
does not satisfy the token threshold. No identifier renaming, literal folding,
near-match or semantic equivalence is supported. Arbitrary statement sequences
which do not form a complete subtree can be missed. Nested matching subtrees
are independently reported, so a large clone can produce overlapping findings.

The extension map explicitly supports Python, JavaScript (`.js`, `.mjs`),
TypeScript, TSX, Bash, Rust and Go. Each grammar has an executable smoke test;
repository-scale coverage currently exercises Python, JS, TS and TSX. Other
extensions (including HTML, CSS, JSON, Markdown and extensionless scripts) are
listed in JSON `unsupported`; they have no generic fallback. Embedded code in
unsupported formats is not scanned. Different grammars never share groups.
Tree-sitter accepts a syntax tree, not necessarily a program accepted by the
language compiler; this is not a correctness or shared-responsibility proof.

`duplicates.toml` sets source roots and explicitly excludes dependency/cache
directories, generated schema/atlas reports, build output and detector fixtures.
Other tests participate. Symlinks are not followed. There is no large-file skip.
Unreadable supported files and parse errors fail closed. A known pinned-grammar
failure in `apps/web/src/loops.test.tsx` has a reviewable, exact-file SHA-256
parse waiver with rationale; JSON lists this file as unscanned. Editing it
invalidates the waiver; fixing it requires removing the stale waiver. Reviewed
parse waivers in the base policy remain available for the baseline scan when
head fixes such a failure. All other detection settings come from head and are
identical for both scans, including when the base predates this gate.

Two files are permitted. A group fails when it spans at least three distinct
files **and** its distinct-file count or total occurrence count increases.
Multiple copies in one file do not count as multiple files. Existing unchanged
debt and unrelated edits pass. Content-preserving renames, moves and line shifts
pass: counts are compared by content identity, independent of paths/lines.
Replacing a removed occurrence with an identical occurrence elsewhere is treated
as relocation. A change shared by three files gets a new fingerprint and fails;
changing only one occurrence usually reduces the old group and passes. A third
file, fourth file or extra occurrence in an already qualifying group fails.

Diagnostics include every file and line range, the fingerprint and shared source
context. JSON includes full shared context for findings, all scan groups,
unsupported files, parse waivers, errors and timing. Resolve findings by extracting
an appropriate abstraction, or explain why these copies have independent
responsibilities in a scoped exception. Similar syntax alone does not justify
forcing unrelated responsibilities into one abstraction.

```toml
[[exceptions]]
fingerprint = "python:<hash from diagnostic>"
files = { "a.py" = 1, "b.py" = 1, "c.py" = 1 }
rationale = "Explain why these exact copies should remain independently owned."
```

Replace `exceptions = []` with these array entries, **before** any
`[parse_exceptions...]` tables. Exceptions require exact content, paths, copy
counts and a nonempty rationale. Wildcards and duplicate entries are rejected.
Growth, moves, content changes or removal make a waiver fail until reviewed and
updated/removed. Exceptions never become directory exclusions automatically.

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
tree size, candidate count and retained matching locations; there is no quadratic
all-pairs comparison. Parse stacks are iterative to avoid Python recursion limits.

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
The conservative subtree contract is selected over structural anonymization:
it can point to exact shared code without claiming semantic equivalence.
