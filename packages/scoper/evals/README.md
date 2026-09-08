# Scoper prompt evaluations

`scoping_cases.json` is the initial prompt eval set. It targets the three desired
properties of a scoped implementation ticket: roughly 500-1000 changed lines,
one cohesive component with backwards compatibility, and interface-first
dependency stacks. It also checks that already completed work is respected.

The line ranges are deliberately stated in the scenario. A scoper cannot infer
changed-line counts reliably from milestone prose alone, and an eval should not
reward fabricated precision. “Approximately” is a sizing heuristic: a cohesive
change near a boundary can still pass, while splitting work into filler tickets
should not.

## Evaluation method

Run each candidate prompt several times against every case. Parse the response
through the production `ScopingPlan` decoder, then have a semantic grader score
each required dimension as pass or fail using the case notes. Report:

- valid-plan rate;
- pass rate for each dimension;
- all-required pass rate per case; and
- variance across repeated runs.

Use the current prompt as the baseline and compare candidate prompts on the same
model and sampling settings. A candidate should not ship if it regresses any
dimension or valid-plan rate. For the first acceptance threshold, require at
least 90% valid plans, 80% all-required passes overall, and a pass on every
case in at least one run. Review failures manually before expanding the set.

Exact output matching is intentionally avoided: names and wording may differ
while the scope is equally good. The grader should judge dependency direction,
component boundaries, compatibility constraints, reuse of existing work, and
whether the proposed tickets are cohesive at the supplied size estimates.

When prompt customization is added, the runner should inject the candidate
instructions through that public seam rather than patching `_INSTRUCTIONS`.
Keep these cases provider-neutral so the same prompt can be evaluated with any
ACP-compatible model.
