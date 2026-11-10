# V2 security architecture snapshot

[Open the map](../index.html). Colored headers identify provenance, not security ratings: code on pinned main, declared contracts, tickets, unmerged candidates, plans/discussion and gaps.

Source: `7abe008a4e93964e8ef9815ed8d2cb7e4ed1d300`. Public evidence captured around 2026-10-30 23:12 UTC. Previous snapshot: `c8792656abc20fb2f5825bfdbedab4240b315237`, captured 2026-10-30 03:57 UTC. Publication does not establish deployment.

Coverage: all 542 regular V2 files and 3 symlinks inventoried; all regular file Git blobs matched. Enumerated 1,267 repository records (418 issues, 849 PRs): 13 populated pages plus an empty final page, 100 per page. All 117 scoped issues and 26 mapped PRs refreshed, including public comments/reviews and thread resolution. Open PR enumeration returned 10 then 0; no maintenance marker or reserved branch existed at capture. Four current candidate heads inspected separately; historical closed candidates remain labeled records. The map has 24 areas, 238 dependency links within the scoped issues (252 explicit prerequisites including external references), 208 evidence records and 116 area source/test anchors.

Material changes: account-derived messaging principals and queue groups; durable VCP acceptance, receipts, transactional event publication and opt-in owned smolvm provisioning; Cedar grant/ceiling code without invocation wiring; merged tenant/per-attempt contracts, new #1254/#1256, closed-unmerged #1218/#1264 and operator candidate #1261. Completion-only #1267 has zero changed files; it does not add implementation. Tenant persistence, broker deployment and attempt credentials remain distinct gaps. Public reviews are attributed reports, including unresolved provisioning/isolation concerns; source supersedes historical findings only where inspected code establishes it.

This is a source/public-ticket assessment, not deployed security qualification or a line-by-line audit. No secrets, private records, other run outputs or deployment settings were inspected. Captured public text omits private work-order links and local path lines. 376 test definitions in the manifest are not passing test counts; runtime suites and real hypervisor smoke were not rerun. See Coverage for selection, inspection method and exclusions.

## Files and maintenance

- `../index.html`: self-contained full-window interactive atlas; embedded graph equals `atlas-data.json`.
- `atlas-data.json`: downloadable evidence graph, source revision, capture time and prior snapshot.
- `source-inventory.json`: complete regular-file manifest, SHA256 hashes and test definitions; identical to the graph's manifest.

Update this atlas in place. Refresh source/records before changing the visible revision/date. Keep all six provenance categories, four views, icons, scrollable diagram and evidence inspector. Pin source anchors to exact commits/lines; contracts and tested libraries are not wired enforcement. Use the existing Pages build/sidebar route `/v2/security/`.

Before publication verify JSON equality, IDs, references, source anchors/counts, relative downloads, production site build and actual desktop/mobile rendering: all views, selections, search failure/recovery, scrolling, legend and documentation navigation. Validation witnessed in a maintenance run belongs in its PR; PR-reported runtime tests are not validation witnessed here.
