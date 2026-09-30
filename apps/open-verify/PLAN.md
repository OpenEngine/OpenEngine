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

Live-agent acceptance also passed on 2026-09-30: Codex inspected a disposable local
greeting page, assessed material UI impact, generated and ran a Playwright journey,
and returned a passed finding with checkpoint screenshots and bounded MP4 evidence.
The manifest paths and recorded sizes were checked against the actual files. This
does not establish reliability for every agent run or real OAuth interoperability.
