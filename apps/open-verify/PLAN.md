# Pre-shipping review fixes

The review identified three gaps outside the earlier 360-test suite:

1. **Recursive JSON equality.** Live exact browser checks and generated tests now
   use one type-sensitive comparator through every object and array. Boolean/number
   mismatches cannot pass through Python container equality.
2. **Navigation after redirects.** Same-entry-origin `browser_open` recordings
   retain the full absolute destination as `navigate_url`, including query and
   fragment. Existing relative `navigate` semantics and origin guards remain.
3. **Cancellation during cleanup.** The journey runner retains cancellation,
   marks the attempt blocked, finalizes cache state and writes the receipt/export
   and result callback before re-raising. Completed step results are preserved;
   strict cache mode remains read-only.

The initial regression run reproduced all three issues: seven failures and two
passing controls. Added coverage also checks nested structure/types, invalid
absolute destinations, workflow manifests and a second cleanup cancellation.
Previously generated Python artifacts must be regenerated to obtain these fixes.

Validation: **387 Open Verify tests passed** (360 existing + 27 regressions),
**21 OpenEngine integration checks passed**, and Ruff passed. The built wheel's
Python sources match the tested source byte for byte. All 27 regressions also
passed against that wheel in an isolated installed environment, including real
Chromium and a standalone exported replay. No model calls, commit or publication
were needed for these deterministic fixes.

---

# Generated HTTP and terminal suites milestone

Goal: save and execute typed backend regression tests with deterministic checks,
authoritative host results and standalone replay without a model, browser or Git.

## Implementation sequence and acceptance

1. **Contracts.** Define closed HTTP/command steps with required status/exit-code
   expectations, optional bounded text/header/JSON checks and per-step deadlines.
   A suite belongs to one planned HTTP or terminal case; every planned completion
   check maps to an assertion-bearing step. Keep existing browser contracts intact.
2. **Deterministic execution.** Execute through the existing engine dispatcher,
   preserving origin and --allow-exec policies. Preflight capabilities/policy,
   stop on first failure, distinguish assertion failure from missing/incomplete
   evidence and preserve checkpoints on timeout/cancellation. No automatic retries.
3. **Exports/replay.** Save a typed JSON spec and editable runnable Python file.
   Execute that saved file in the original run, and use the same checked runtime
   in standalone replay. Keep model-provided values as data. Document dependencies,
   project selection, origin allowance, command opt-in and exit codes.
4. **Runner integration.** Expose run_backend_test only for planned HTTP/terminal
   execution. Enforce current-case ownership, completion coverage and one execution
   per case. Finalize findings from host results; change-mode backend findings must
   cite actual generated-test execution. Preserve manifest schema 1 and attachments.
5. **Acceptance.** Exercise real HTTP and command fixtures, passing/failing/blocked
   results, nonzero expected exits, JSON type/path errors, truncated evidence,
   unsafe paths/origins, injection-shaped data, deadlines, cancellation, standalone
   replay, case ownership and report/publication compatibility. Run regressions,
   lint, isolated base-wheel replay, built CLI and live-model generation.

Scope: public HTTP requests and noninteractive argv commands with literal test
data. Commands retain the host's OS authority and require --allow-exec. HTTP
redirects are not followed. Text assertions use complete bounded UTF-8 output;
large or invalid text cannot silently pass. Authentication/secret substitution,
response chaining, interactive terminals and automatic replay caching remain
separate work. Replaying a test repeats its application side effects.

## Progress

- All five stages are complete. Full regression: **360 passed**, including
  **38 new backend-suite tests**. OpenEngine consumer/publication/upload
  integration: **21 passed**. Ruff passed.
- The base wheel built and installed offline in a fresh environment with neither
  Playwright nor LangGraph. HTTP and terminal tests executed and replayed there;
  a failed HTTP expectation replayed with exit 1, and command replay without
  --allow-exec returned exit 2. Built CLI runs passed for both interfaces with
  manifest schema 1 and generated test attachments.
- Live Codex ACP independently generated, executed and passed one HTTP health
  regression (status + typed JSON) and one terminal greeting regression (exit code
  + exact output). Both saved Python exports replayed with exit 0 without a model.
- Verified literal source generation, POST bodies, redirects without following,
  expected nonzero exits, combined output, JSON/null/type/path failures, complete
  text beyond preview bounds, oversized/invalid UTF-8 evidence, origin and command
  permissions, unsafe cwd, stop-on-failure, total/operation deadlines, cancellation
  checkpoints, case/check ownership, one execution per case and mixed browser/
  backend bundle support. Host results finalize generated backend findings.
- Reviewed engine/runner boundaries, checked dispatch, complete evidence loading,
  saved-artifact execution, standalone cleanup, manifest compatibility and prompt
  consistency. Tested stage and applied sources match; the final wheel matches
  the applied Python sources byte for byte. No commit or publication performed.

Remaining roadmap items: frame targeting/additional engines and durable resume.
Visual redaction/baselines, authenticated/fill replay caching, authenticated backend
suites, response chaining and interactive terminals need separate designs.

---

# Visual judgments milestone

Goal: evaluate explicit visual requirements from fresh screenshots in an independent
judge session, with deterministic capture/limits and no reuse of prior verdicts.

## Implementation sequence and acceptance

1. **Contract.** Add opt-in `mode: "visual"` to assertion steps; retain text-based
   semantic judgments by default. Reject a visual mode combined with an exact
   check, and keep completion coverage, budgets and immutable plans unchanged.
2. **Evidence.** Capture a fresh viewport PNG through the engine dispatcher.
   Record its path, dimensions, scope and digest. Validate bounded image bytes
   from a regular file inside the run's artifact directory before model input.
3. **Transport.** Extend ACP responses with an optional typed PNG attachment.
   Require advertised image support before a request; preserve the same image
   through schema repair and native-tool recovery, under existing call/deadline
   budgets. Never substitute a path string or text snapshot for an image.
4. **Judge isolation.** Use a distinct visual executor method and a fresh session
   containing only the requirement, current URL, image metadata and image bytes.
   Interpret holds/fails/inconclusive as existing judgments do. Missing capture,
   invalid images, unsupported transport or executor support must never pass.
5. **Replay/reporting.** Always capture and judge again after cached actions.
   Keep visual verdicts and image bytes out of recordings; export a live-judgment
   barrier. Retain the exact screenshot and its metadata with step evidence.
6. **Acceptance.** Test schema compatibility, real visual fixtures, current
   screenshots, verdicts, corrupt/oversized/unsafe files, unsupported transports,
   repairs, cancellation, isolation, replay and export. Run full regressions,
   OpenEngine integration, lint, built CLI and installed-wheel live acceptance.

Scope: explicit viewport assertions on Chromium, using providers that advertise
image input. Full-page stitching, screenshot baselines, image tools for the actor
and pixel redaction are separate work. Existing screenshot artifacts stay local
unless a visual assertion explicitly selects pixels as model evidence. Visual
assertions can send visible application content to the configured provider.

## Progress

- All six stages are complete. Full regression: **322 passed**, including
  **37 new visual-judgment tests**. OpenEngine consumer/publication/upload
  integration: **21 passed**. Ruff passed.
- The wheel built offline and installed in an isolated environment without
  LangGraph. Its Python sources, the tested staging snapshot and the applied
  repository sources matched byte for byte.
- Built CLI cold and strict runs passed through actual ACP image content blocks,
  Chromium screenshots, visual step receipts, exported verification barriers and
  unchanged manifest schema 1.
- Live Codex ACP judged a canvas whose color was absent from accessibility text.
  The blue swatch passed cold (**2 actor calls + 1 visual judge call**) and after
  strict replay (**0 actor calls + 1 fresh visual judge call**). A planted red
  swatch failed after the same strict action-cache hit (**0 actor calls +
  1 fresh visual judge call**). The exact captured images remain in the run
  artifacts; the defect screenshot was also inspected directly.
- Verified explicit schema selection, rejection of mixed exact/visual checks,
  fresh viewport capture, all three judge verdicts, independent input, image
  checksums/pixel data/dimensions, size limits, traversal/symlink/non-regular-file
  refusal, tampering/missing evidence, unsupported engine/executor/provider,
  schema repair, native-tool recovery, model-call limits, timeout/cancellation,
  cache reuse, fresh failed-judge invalidation and honest replay exports.
- Reviewed evidence loading, transport capability checks, fresh sessions,
  immutable image reuse during repair, reporting and compatibility. Applied
  files only after baseline hashes matched. No commit or publication performed.

Generated HTTP/terminal suites are complete in the milestone above. Remaining
roadmap items include frame targeting/additional engines and durable resume. Visual coverage remains viewport-only;
pixel redaction and screenshot baseline comparisons are separate work.

---

# Semantic node IDs and screen diffs milestone

Goal: give the acting model engine-issued control references and explicit screen
changes, while keeping dispatch deterministic, stale references unusable and
assertions independent.

## Implementation sequence and acceptance

1. **Observation contract.** Extend browser observations with a versioned semantic
   control table, an observation ID and bounded screen diffs. Use Chromium's own
   accessibility roles/names/states. Preserve the existing full current snapshot
   for judgments and compatibility. Bound large/incomplete observations explicitly.
2. **Node identity.** Assign attempt-local IDs to real DOM nodes identified by
   Chromium backend node identity. Retain IDs across unchanged observations and
   moves of the same physical element; replacement and navigation invalidate old
   references. Repeated identical observations retain the observation ID.
3. **Checked node actions.** Add click/fill/press by node ID plus observation ID.
   Re-observe immediately before dispatch and reject unknown, stale or ineligible
   references. Bind Playwright to the exact physical element, including duplicate
   accessible names. Never accept model-provided selectors or JavaScript.
4. **Diffs and model integration.** Report added/removed/changed controls and
   bounded text changes relative to the preceding screen; reset on navigation.
   Teach the actor to prefer issued references and refresh after stale errors.
   Keep the semantic judge on the full fresh snapshot, without diffs or history.
5. **Replay and exports.** Convert node operations to a stable ordinary locator
   only when it uniquely resolves to that same element. Never persist transient
   IDs in recordings. Ambiguous node actions keep a replay barrier and bypass
   recording. Include canonical semantic state in cache guards and bump the
   engine replay revision; do not loosen exact-match invalidation.
6. **Acceptance.** Test duplicate names, reorder/replacement, state/text changes,
   navigation and cross-context references, detached/disabled nodes, truncation,
   stale-action refusal, fresh judges, replay/export and cache invalidation.
   Run full regressions, consumer integration, lint and installed-wheel acceptance.

Scope: Chromium main-document controls, including open shadow DOM where the
accessibility tree exposes the element. Frame targeting, visual-only controls,
semantic fuzzy matching and relaxed cache guards remain separate work. Node IDs
are runtime references, not stable identifiers across runs or persistent selectors.

## Progress

- All six implementation stages are complete. Final applied-source regression:
  **285 passed**, including **23 new semantic-reference tests**. Ruff passed.
- OpenEngine consumer, publication and upload integration: **21 passed**. Manifest
  schema 1 remains unchanged. The final wheel built offline, installed in an
  isolated environment without LangGraph, and its Python sources matched the
  applied repository files byte for byte.
- Built CLI cold and strict runs passed with scripted ACP and real Chromium.
  Live Codex ACP used `browser_click_node` on the cold run (**2 actor calls +
  1 judge call**); strict replay used the engine-resolved ordinary locator
  (**0 actor calls + 1 fresh judge call**). Both exact and semantic assertions
  passed. Runtime receipts retained the node action and its screen diff.
- Verified duplicate names, unchanged/reordered physical nodes, identical
  replacements, same-content navigation, URL-only changes, cross-context tokens,
  detached targets, disabled/read-only controls, fills, key presses, state/text
  diffs, open shadow DOM, bounded output and accessibility-reader failure.
- Unique node actions export/cache ordinary locators. Ambiguous actions pass live
  but produce replay barriers and bypass recording. Transient references do not
  enter recordings or generated selectors; cache guards include canonical
  semantic state and the local engine replay revision is now 2.
- Reviewed dispatch, observation lifetime, cache compatibility, judge isolation
  and export boundaries. Added the URL-only diff regression during review.
  Source files were applied only after baseline hashes matched. No commit or
  publication performed.

Visual judgments are complete in the milestone above. Frame targeting, additional
engines and durable resume remain separate work; generated backend suites are
complete above.

---

# Replay cache milestone

Goal: reuse verified browser action sequences without acting-model calls, while
running all assertions again and retaining actual execution evidence.

## Implementation sequence and acceptance

1. **Storage and identity.** Add bounded, versioned JSON recordings with atomic
   private-file writes. Key by project, exact accepted journey/checks, engine
   replay revision, runtime versions, tool schemas and origin policy. Store
   observation hashes rather than screen text. Cache data stays outside bundles.
   Accept only complete recordings from cases whose assertions and cleanup pass.
2. **Recording.** Capture each successful act's initial screen and every checked
   action's before/after screen hashes. Preserve exact arguments and validate the
   recording schema. Do not save failed, interrupted, truncated or unsupported
   traces. Initially bypass authenticated journeys and any step using field fills
   so the persistent cache does not introduce stored form values or login state.
3. **Checked replay.** Obtain a fresh observation before replay and before each
   recorded action. Reuse the same engine dispatcher, limits, trace exporter and
   evidence receipts as live execution. Compare the resulting screen after every
   action. Exact and semantic assertions always execute afresh; cache entries
   never contain assertion verdicts or acting-model summaries.
4. **Invalidation and fallback.** Auto mode can replace an invalid/missing entry
   through live execution; stale-state fallback is allowed only before the
   current step dispatches any recorded action. A later mismatch or action error
   blocks the case and invalidates the recording without repeating side effects.
   A failed fresh assertion invalidates the recording and retains the failure.
5. **Controls and diagnostics.** Add auto (CLI default), strict, refresh and off
   modes, plus a configurable cache directory. Strict mode blocks absent/stale
   recordings without calling the actor, but still runs fresh semantic judges.
   Report hit/miss/stale/bypass/refresh and writes/invalidation in step diagnostics
   and progress, preserving manifest schema 1. Programmatic runners stay uncached
   unless given a cache instance.
6. **Acceptance.** Exercise two real-browser runs with zero acting-model calls on
   the second, fresh judges, before/after-action staleness, changed plans/policies,
   failed assertions, malformed/oversized files, strict and disabled modes,
   cancellation, privacy exclusions and exported replay. Run the full Open Verify
   suite, OpenEngine consumers, lint and built-wheel acceptance. Review the diff.

Exact snapshot matching is deliberately conservative: dynamic text can cause a
miss or stale result, and screen hashes do not describe hidden backend state.
Journey instructions are not fuzzy-matched; case IDs and titles are not cache keys. Stable entry URLs and identical
journeys are required. Semantic node IDs/screen diffs were completed in the milestone above.

## Progress

- All six implementation stages are complete. Final applied-source regression:
  **262 passed**, including **36 new replay-cache tests**. Ruff passed. Earlier
  intermediate runs passed 253 total tests and then 33 focused cache checks.
- OpenEngine consumer/publication integration: **16 passed**; manifest schema 1
  remains unchanged and cache files are excluded from attachments.
- The wheel built offline and installed in an isolated environment without
  LangGraph. Built-CLI cold and strict runs passed through scripted ACP, actual
  Chromium actions, assertions, generated exports and manifests.
- Live Codex ACP acceptance against a disposable cart fixture passed twice using
  the installed wheel: the cold run made **2 actor calls + 1 judge call**; strict
  replay made **0 actor calls + 1 fresh judge call**, with the same passing exact
  assertion. Both runs retained step receipts and screenshot evidence.
- Verified initial-stale fallback before dispatch; stopping after partial replay
  without duplicate actions; failed fresh assertions and failed refreshes
  invalidating recordings; read-only strict mode; bounded malformed-file reads;
  atomic replacement, symlink rejection, cancellation, privacy exclusions and
  standalone replay of the actual cached click plus exact assertion.
- Reviewed compatibility identity, dispatch boundaries, private storage, fresh
  judgment context, trace export and interrupted-run reporting. Source files were
  applied only after baseline hashes matched. No commit or publication performed.
- Compatibility matching remains exact. There is no planner-output cache, and
  repeated free-form requests may yield different plans and thus cache misses.

The following semantic node ID/screen diff milestone is complete above. Later
milestones include additional engines and durable resume; generated HTTP/terminal
suites are complete above. Authenticated/fill recording requires a separate design for
session-bound data and values before broadening cache eligibility.

---

# Structured act/assert milestone

This milestone follows the completed runner/executor/engine migration below.
It adopts the e2e separation of bounded action reasoning and independent assertions
without adding LangGraph orchestration.

## Implementation sequence

1. Define closed schemas for browser journeys, act/assert steps, actor responses,
   judgments and step results. Validate fixed assertion coverage before execution.
2. Add a deterministic journey runner. Isolate browser contexts by case and LLM
   conversations by act step. Enforce deadlines, action/model budgets and loop
   guards. Count schema repair against the same model budget.
3. Execute exact assertions in the engine. Evaluate semantic assertions in a fresh
   evidence-only model session. Refuse success when evidence is inconclusive.
4. Integrate `run_journey(case_id)` into the planning/setup loop. Derive findings
   from host results and preserve interruption checkpoints in the manifest.
5. Export observed actions and deterministic assertions. Insert explicit replay
   barriers for semantic/unexecuted checks and uncertain or oversized traces.
   Preserve authentication, origin enforcement, media and legacy generated tests.
6. Make network fixtures portable using an ephemeral localhost destination port
   classified as external in tests. Keep production origin rules and actual
   denied-request/handshake counters unchanged.
7. Verify real-browser passing/failing/blocked journeys, model/engine boundaries,
   exported replay, cancellation and OpenEngine consumers. Run bounded live ACP
   action and judge acceptance, then inspect the complete source diff.

## Progress and verification

- Steps 1–6 implemented. Legacy plans remain supported; structured browser plans
  use the new step runner. No CLI flag or manifest schema change is required.
- First full regression run: **212 passed**, including the 37 network fixtures
  that previously could not bind on macOS. Ruff passed.
- Live Codex ACP acceptance (2026-10-02): a disposable local cart journey passed
  an act, an exact assertion and an independent semantic assertion. The actor
  used two model calls and one click; the judge used one model call. This is a
  live provider check, not a claim of general model reliability.
- Final applied-source regression: **226 passed**. After the last prompt and
  JSON replay consistency adjustments, all **64 affected workflow, journey and
  generated-test checks** passed. Ruff passed.
- OpenEngine consumer/publication integration: **16 passed**. Manifest schema and
  existing CLI flags are unchanged.
- The standalone wheel built offline and installed in a fresh environment with
  the browser extra and no LangGraph. Its real `ov` CLI completed Codex planning,
  fixed act/assert execution and independent judgment on the local cart fixture;
  exit status was 0 and the manifest was passed. The semantic replay barrier was
  explicitly listed in manifest omissions.
- Built-wheel model-free media acceptance produced a valid **3,513-byte GIF**.
  Exact browser replay passed; a failing exact check replayed with failure exit
  code 1. Cancellation during a step or media encoding retained completed
  execution evidence and marked unfinished work accurately.
- Source diff reviewed for context ownership, model-call repair accounting,
  closed tool dispatch, assertion coverage, authentication, export barriers and
  partial manifests. Changes applied only after baseline hashes matched.
  No commit or publication was performed.

Historical follow-up list after the act/assert milestone (replay caching is
implemented by the newer milestone above): automatic replay caching and invalidation,
semantic node IDs and screen diffs, visual model judgments, additional engines,
HTTP/terminal generated suites and durable resume. Independent semantic judgment
and action trace export are implemented here; earlier follow-up lists below are
historical.

---

# Runner, executor, and engine migration

Requested architecture: retain Open Verify's LLM planning layer and implement the
verification runtime as a deterministic Python runner, with replaceable executors
and application engines. LangGraph remains available to OpenEngine's outer
workflows; Open Verify does not require it to orchestrate verification.

## Scope and boundaries

- The user request and inspected requirements define the scope. The planning
  executor proposes cases and expected outcomes. The runner fixes the accepted
  plan for the run.
- The runner owns state transitions, decision budgets, case order, retries,
  cancellation, evidence validation, generated-test results, and final status.
- An executor receives a detached context and returns a typed decision. It cannot
  mutate runner state. The built-in executor builds prompts and manages case
  sessions over the existing provider-neutral ACP transport.
- An engine advertises tool schemas and executes checked application operations.
  Browser, HTTP, process, and discovery adapters contain no model decisions.
- Generated Playwright assertions remain deterministic and authoritative. Model
  findings for exploratory cases remain evidence-backed interpretations; this
  migration does not claim to introduce an independent semantic judge.
- Keep CLI flags, generated source, assisted login, network-origin restrictions,
  publication behavior, exit codes, and manifest schema compatible.

## Execution stages

1. **Baseline and change isolation.** Read repository instructions; snapshot files
   before editing and compare hashes before applying changes to avoid overwriting
   concurrent work. Run the existing suite. No git tool is exposed in this task,
   so use filesystem comparisons and make no branch, commit, or publication.
   Acceptance: existing failures and supported local checks are recorded.
2. **Plain runner.** Replace StateGraph orchestration with an explicit bounded
   decide/apply loop. The runner alone merges state updates. Preserve partial
   reports on exceptions/cancellation, terminal states, and budget accounting for
   refused decisions. Keep compatibility imports from `workflow.py`.
   Acceptance: existing workflow/CLI tests plus cancellation and state-progression
   tests pass without importing LangGraph.
3. **Executor contract.** Extract prompt construction, context compaction and ACP
   session selection from orchestration. Introduce a typed context and executor
   protocol, with a built-in adapter for existing `decide(prompt)` agents. Scope
   execution context to one case and reset provider sessions between cases.
   Acceptance: custom executors work without ACP; context mutation cannot alter
   the fixed plan; existing provider recovery and case-session tests pass.
4. **Engine contract.** Expose tool capabilities, checked dispatch, environment
   context and lifecycle through an engine protocol. Put argument validation,
   stage authorization and evidence receipts in a reusable dispatcher. Adapt the
   local implementation without changing browser/network/process behavior.
   Acceptance: a non-local test engine can run through the same runner; unknown
   tools, extra arguments and discovery-time mutations fail before execution.
5. **Verification boundary.** Isolate generated-journey execution policy from the
   agent loop. Keep current-case ownership, fixed check coverage, diagnosis for
   retries, the two-attempt limit, and actual test results authoritative.
   Acceptance: false agent success cannot override a generated failure; exports,
   authentication and media checkpoints retain their existing behavior.
6. **Dependencies and documentation.** Remove Open Verify's direct LangGraph
   dependency and regenerate its standalone lockfile without upgrading retained
   dependencies. Keep `langgraph-acp` as transport; its name is not a runtime
   requirement to use LangGraph. Document the contracts, composition and limits.
   Acceptance: CLI import works with LangGraph imports prohibited; standalone
   package metadata and lockfile agree; OpenEngine integration remains compatible.
7. **Acceptance and review.** Run the complete suite, distinguish baseline
   environment failures, run relevant OpenEngine consumer tests, lint changed
   modules, inspect the filesystem diff and verify generated-test replay.
   Acceptance: no unexplained new failures; report exactly which checks ran and
   which require another environment or live provider credentials.

## Explicit follow-up features

Automatic action recording/replay, semantic node IDs and screen diffs, a separate
LLM judge, generated HTTP/terminal suites, mobile engines and durable resume are
future consumers of these contracts. They are not prerequisites for removing
LangGraph or claims of this migration. Retain existing generated-test replay.

## Progress

- Baseline: 187 collected tests; 150 passed and 37 failed. All 37 failures involve
  fixtures binding to `127.0.0.2`, unavailable on this macOS host. No network alias
  or other host configuration was changed.
- Stages 2–6 implemented: `VerificationRunner` uses a plain Python loop;
  `StepExecutor` and `DecisionContext` isolate reasoning from live state;
  `Engine`, `ActionDispatcher` and `LocalEngine` separate checked operations;
  `CaseVerifier` owns generated-test and finding validation. Compatibility aliases
  retain `workflow.Verification`, `tools.LocalTools` and `auth.LoginRequest`.
- Stage 7 completed with the baseline environment limitation: the complete
  migrated suite has **161 passing tests and the same 37 failures**, all reporting
  unavailable `127.0.0.2` binding. All 11 new architecture checks pass. The suite
  includes real Chromium execution, assisted-login fixtures, generated Playwright
  replay, cancellation, evidence validation and the scripted ACP subprocess.
- OpenEngine consumer checks: **16 passing tests** in
  `tests/test_open_verify_node.py` and `tests/test_verification_publication.py`.
  These simulate publication and do not upload or post to GitHub.
- Ruff passes for the Open Verify source and changed tests. `uv lock --check
  --offline` passes. Regeneration removes 27 packages including LangGraph, with no
  retained package version changes. The wheel builds and includes all new modules.
- Built-wheel acceptance: installed in a separate temporary environment containing
  neither LangGraph nor Playwright; the real CLI completed a scripted ACP → local
  terminal action → evidence → passed manifest journey. No model account was used.
- Source review confirms browser/HTTP/process operation bodies and the default
  agent instructions are unchanged. New tests prohibit concrete engine/provider
  imports from the core, prohibit LangGraph imports through the CLI, exercise a
  custom executor and engine, and verify detached context, rejected actions,
  cancellation, decision budgets and revalidation of executor output.
- No live Codex/Claude model acceptance run or real PR publication was performed.
  Full network-policy acceptance on this host still requires the loopback fixture
  address; the application policy and machine network settings were not changed.

---

The sections below are historical plans and validation records. Their references
to LangGraph and older media formats describe earlier implementation stages.

# Change-based verification — implementation plan

Goal: point Open Verify at a change and produce relevant, reproducible end-to-end tests and visual evidence that OE can attach to a merge request.

Proposed interface:

```sh
ov --project . --base origin/main --head HEAD --output ./verification
```

An optional feature description narrows the scope. Revision inputs must match the checkout being tested; working-tree changes require an explicit option.

1. **Understand the change.** Read the diff and project instructions, discover how to run the app, and map changed behavior to affected user journeys. Record the revisions and environment used.
2. **Decide what needs verification.** Extend the LangGraph flow with impact assessment and a targeted test plan. Generate tests for meaningful behavioral changes; capture media when it demonstrates material end-user impact. Allow a `skipped` outcome with a reason and no attachments. Uncertain impact or failed setup must not silently become a skip or pass.
3. **Generate and run tests.** Start with Playwright, which the CLI already uses. Save runnable tests, fixtures, dependencies, and a rerun command in the output bundle. Execute them against the app and report passed, failed, or blocked outcomes. Keep an interface for a later Maestro runner; retain existing terminal/HTTP verification for backend cases.
4. **Capture useful evidence.** Produce focused screenshots and short videos of the affected journeys, linked to their tests. Capture failures when useful. Before/after comparisons are optional when both revisions can be run. Finalize browser recordings before processing files.
5. **Enforce the video limit.** Produce MP4 clips, trim or compress toward a 9 MB target, and verify every final file is strictly below 10,000,000 bytes. If necessary, split into useful clips or omit the video with an explicit reason. Never include an oversized video in the publication manifest.
6. **Expose an OE contract.** Write a versioned `manifest.json` containing outcome, change references, impact/skip reasons, test results and rerun commands, plus artifact types, relative paths, sizes, and associated test IDs. OE consumes this bundle and handles MR publication. Internal logs and traces remain diagnostic artifacts.

First milestone: Playwright tests + screenshots + bounded videos + manifest. Keep Python, LangGraph, and provider-neutral Codex/Claude integration; keep the CLI independently extractable. Maestro and Docker execution follow later.

Acceptance checks: a representative UI change produces executed tests and relevant media; a change without meaningful behavioral impact produces no attachments; a setup failure is reported as blocked; failing tests retain useful evidence; every published video meets the byte limit and every manifest path resolves within the bundle.

Implementation references: [Playwright video lifecycle](https://playwright.dev/python/docs/api/class-video), [Maestro local recording](https://docs.maestro.dev/maestro-flows/workspace-management/record-your-flow).

## Implementation status

## Reliability and evidence acceptance milestone

1. Preserve the existing typed Playwright compiler and OE manifest. Recover one
   native-tool protocol violation using a fresh agent session and the recorded
   task context; keep the retry bounded and retain the final blocker.
2. Add explicit screenshot checkpoints to generated journeys so a bundle can
   show both the initial UI and the changed state. Print generated artifact paths
   and omissions as each journey completes.
3. Run a real local browser acceptance test, including standalone replay. Require
   an executed test, checkpoint screenshots, a valid MP4 below 10,000,000 bytes,
   and an internally consistent manifest. Verify failure and skip outcomes too.
4. Record validation results and limitations here. A scripted planning fixture
   validates the execution pipeline; it does not prove live model reliability.

Implemented: change inputs, impact/skip assessment, generated and executed
Playwright journeys, named checkpoint screenshots, bounded MP4 export, and the
versioned OE manifest. Generated tests can be replayed without an agent. The browser
extra bundles an encoder; unavailable or oversized video is omitted with a reason.
See README.md for the CLI and artifact contract. Maestro, Docker, before/after
environments, and generated backend suites remain follow-up work.

Validation (2026-09-30): 36 focused checks pass, including real Chromium execution,
standalone replay, passing and failing journeys, cancellation, skipped changes,
manifest path validation, and strict video size checks. The passing local fixture
produced an executable test, initial/result screenshots, and a 5,251-byte MP4 using
the bundled encoder. The failing fixture retained an 8,343-byte MP4 and screenshots.
These fixtures use scripted planning decisions; live-agent acceptance is tracked
separately and must not be inferred from their success.

Implemented (2026-10-01): per-file paginated diff inspection beyond the initial
preview; authenticated generated-test replay with `--login`; an opt-in OE
`OpenVerify` workflow node; validated manifest consumption and PR-head checks;
and a github.com prerelease-asset uploader with PR comments. No credentials,
diagnostic logs, or browser state are published. Videos retain the strict 10 MB
limit. Storage is injectable; release publishing is disabled until a deployment
supplies the node and uploader. Local adapter/consumer tests simulate GitHub
responses; live release creation, uploads and PR rendering remain a deployment
acceptance check, not a claimed test result.

Live-agent acceptance also passed on 2026-09-30: Codex inspected a disposable local
greeting page, assessed material UI impact, generated and ran a Playwright journey,
and returned a passed finding with checkpoint screenshots and bounded MP4 evidence.
The manifest paths and recorded sizes were checked against the actual files. This
does not establish reliability for every agent run or real OAuth interoperability.
