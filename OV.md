# OpenEngine live QA

Use this as a project-specific setup recipe for Open Verify. For a PR whose
checkout does not contain it, copy it with `--setup-file OV.md` and
`--setup-file scripts/ov-smoke.py`.

## Application smoke journey

Start the PR's real backend and freshly built web client, then use the browser
to create a uniquely named dummy WorkOrder. Verify its prompt, repository and
initial state through the UI, reload, and confirm the same WorkOrder persists.
Capture the creation and resulting detail page in one GIF and export the
executed browser journey. Do not substitute an existing pytest or Playwright
spec for this journey. Keep any existing-test run as supporting evidence.

The creation form identifies the WorkOrder through its task prompt; its display
name is derived automatically. Use one unique dummy prompt, accept the derived
name, and record the created run ID. Do not require a separate name field.
Inspect the form before planning and keep creation, explicit detail assertions,
reload, and persistence assertions as separate journey steps. Mark this case
`coverage=regression`; mark scoped-ticket contract checks `coverage=changed_behavior`.
For the submitted prompt and the documented `awaiting human review` status,
use exact `expect_text` checks in assert steps, rather than a model judge.
Verify the repository separately. After reload, repeat the exact prompt and
status checks and repository assertion. The unique prompt identifies this dummy
record; record its run ID as evidence without hardcoding that ID into a replay
that creates a new record.
Set `remember_as="created"` on the pre-reload prompt assertion. After reload,
check identity with `check={"kind":"expect_same_url","baseline":"created"}`.
For repository comparisons use a semantic assertion with `compare_to="created"`,
so the judge compares recorded repository evidence instead of guessing its history.

WorkOrder submission starts execution. Do not run the production implementation
workflow against a real repository just to test the form. Prefer the isolated
browser harness described in `apps/web/e2e/README.md`. It serves the real UI,
API, stores and graph runtime, with scripted ACP providers and fake GitHub
responses. This verifies application wiring; it does not verify real model or
GitHub behavior. Disclose those substitutions in the plan and report.

## Prepare the environment

1. Inspect Python and Node requirements and install the checkout's declared
   dependencies (`uv sync --locked --all-packages` and
   `npm --prefix apps/web ci`). Build its client with
   `npm --prefix apps/web run build`. Never serve another checkout's `dist`.
2. Inspect `apps/web/e2e/harness.ts` for environment provisioning and
   `apps/web/e2e/harness/server.py` for launching the composed server. Reuse
   their setup contract, not the existing browser test assertions.
3. Launch the supplied helper as a managed process, using the PR-local Python:

   ```sh
   .venv/bin/python scripts/ov-smoke.py --port 0
   ```

   It creates disposable directories and composes the existing browser harness
   with **OV application smoke**, a real graph consisting of a human review
   gate. Creating a WorkOrder starts that graph and waits for a person; it runs
   no model, workspace Git operations or implementation/publishing work. The
   helper works with older harness versions and does not edit product sources.
   It does not prove that the default implementation-review graph works.
4. The server prints `ENGINE_E2E_URL=http://127.0.0.1:<port>`. Use its printed URL
   and check `/api/health` and `/api/config` before opening the browser.
   When planning before the free port is known, use journey URL `/runs/new`.
   After readiness, call `run_journey` with `case_id` and `base_url` equal to the
   printed origin. Never send an unresolved relative URL directly to a browser.
   Select
   **OV application smoke** when creating the dummy WorkOrder. Verify it reaches
   human review and survives reload; do not accept or delete it unless requested.
5. To test the full implementation-review graph separately, launch the original
   harness server and follow `apps/web/e2e/README.md`: that mode needs a Git fixture with a local
   origin and an `ENGINE_FAKE_SCRIPT` provider script. Inspect the harness setup
   and workflow examples; do not substitute its existing assertions for OV's
   journey. Disclose fake provider/forge responses, and claim completion only
   when the expected final state is actually observed.

Do not copy personal `.env`, deployment configuration, databases or agent
credentials into this environment. The harness uses isolated defaults and
does not need live Slack or GitHub. Stop managed services at the end; retain
test evidence separately from the disposable environment.

## Backend change coverage

For scoped-ticket persistence changes, also generate independent public
state-store API checks in temporary SQLite storage. Verify dependency identity,
approval filtering, reopen persistence and rejection. Do not merely invoke
`tests/test_scoped_tickets.py`. This behavior is not established by creating a
WorkOrder in the UI; the smoke journey and contract checks serve different
purposes. Use `--max-cases 2` when requesting both.
Put the independent Python harness in the backend command's stdin using
`.venv/bin/python -`, so its exported test includes everything needed to replay
against another installed checkout. Print the observed ticket identities,
relationships, approval states and queue membership after asserting each stage.

If the application cannot start, retain its logs and report the live journey as
blocked. Passing supporting tests cannot turn that blocker into a pass.
