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

The first milestone is implemented: change inputs, impact/skip assessment, generated
and executed Playwright journeys, focused screenshots, bounded MP4 export, and the
versioned OE manifest. Generated tests can be replayed without an agent. MP4 export
requires ffmpeg with libx264; unavailable or oversized video is omitted with a reason.
See README.md for the CLI and artifact contract. Maestro, Docker, before/after
environments, and generated backend suites remain follow-up work.
