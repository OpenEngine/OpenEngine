# V2 security architecture snapshot

[Open the map](../index.html). Colored headers identify provenance, not security ratings: code on pinned main, declared contracts, tickets, unmerged candidates, plans/discussion and gaps.

Source: `d1064d8c6e41eed39443788aef8fd7e5c9c3214c`. Public evidence captured around 2026-11-09 16:25 UTC. Previous snapshot: `7abe008a4e93964e8ef9815ed8d2cb7e4ed1d300`, captured 2026-10-30 23:12 UTC. Publication does not establish deployment.

Coverage: all 626 regular V2 files and 3 symlinks inventoried; all regular file Git blobs matched the untruncated public tree. Enumerated 1,346 repository records (439 issues, 907 PRs): 14 populated pages and an empty final page, 100 per page. Refreshed all 121 scoped issues and 43 mapped PRs, including public comments/reviews and thread resolution. Open PR enumeration initially returned 22 then 0; the final first page returned 23 after unrelated blog PR #1346 appeared; no open maintenance marker or reserved remote branch. Three new candidate patches inspected; four unchanged candidate heads retain explicitly carried-forward analysis. The map has 24 areas, 238 dependency links within the scoped issues (252 explicit prerequisites including external references), 312 evidence records and 113 area source/test anchors.

Material changes: tenant-table library and GE request-scoped SQLite persistence; Tools tenant-leading schema; owned destroy and host worktrees; executable durable workspace daemon, ACP process fencing, cancellation and fake steering; a separate optional runtime broker with ACLs, TLS-first VCP connections and credential lifecycle code. Managed VCP execution commands and credential handoff remain unwired; public broker auth, per-attempt tool authority, VCP/CXN tenancy and PostgreSQL enforcement remain distinct gaps. Added #1292/#1294/#1295/#1304 and candidate #1311/#1313/#1323; updated merges/closures and attributed plans. Commentary and unresolved reviews do not establish deployment or an independently reproduced exploit.

This is a public source/ticket assessment, not deployed security qualification or a line-by-line audit. No secrets, private advisories, other run outputs or deployment settings were inspected or copied. Private work-order links and local path lines are omitted from captured public text. External tracker destinations in #1294 are excluded. 580 test definitions are not passing test counts; runtime suites and real privileged/model/hypervisor smoke were not rerun. Historical source anchors retain their exact revision and are explicitly labeled. See Coverage for methods and exclusions.

## Files and maintenance

- `../index.html`: self-contained full-window interactive atlas; embedded graph equals `atlas-data.json`.
- `atlas-data.json`: downloadable graph, source revision, capture time and previous snapshot.
- `source-inventory.json`: complete regular-file manifest, SHA256 hashes and test definitions; identical to the graph's manifest.

Update this atlas in place. Refresh source/records before changing the visible revision/date. Keep all six provenance categories, four views, icons, scrollable diagram and evidence inspector. Pin source anchors to exact commits/lines; contracts and tested libraries are not wired enforcement. Use the existing site build/sidebar route `/v2/security/`.

Before publication verify JSON equality, IDs, references, source anchors/counts, relative downloads, production site build and actual desktop/mobile rendering: all views, selections, search failure/recovery, scrolling, legend and documentation navigation. Validation witnessed in a maintenance run belongs in its PR; PR-reported runtime tests are not validation witnessed here.
