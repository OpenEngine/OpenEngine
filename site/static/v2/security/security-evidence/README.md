# V2 security architecture snapshot

[Open the map](../index.html). Its colored headers identify evidence provenance, not security ratings: code, contracts, tickets, unmerged PRs, plans/discussion and gaps. Select a node for its boundary, source and captured records.

Source: `c8792656abc20fb2f5825bfdbedab4240b315237`. Evidence assembled on 2026-10-30 at 03:57 UTC. Publication does not refresh that snapshot.

Coverage: all 489 regular V2 files and 3 symlinks inventoried; regular files matched the Git tree without missing or mismatched blobs. All 1,244 repository issue/PR records were enumerated; 115 scoped issues and selected PR/discussion records are mapped. Six candidate heads were inspected separately from main. The map contains 24 areas, 236 explicit issue dependency links, 177 evidence records and 96 source/test references.

This is a source and public-ticket assessment, not runtime security qualification or a line-by-line independent audit. No deployed configuration, secrets, private discussion or unpublished branch was inspected. The 269 test definitions in the manifest are not passing test counts. The Coverage view lists the method and exclusions.

## Files and maintenance

- `../index.html`: self-contained interactive atlas with embedded evidence; no external runtime assets.
- `atlas-data.json`: downloadable copy of the same evidence graph.
- `source-inventory.json`: complete regular-file manifest, hashes and discovered test definitions.

Keep the HTML's `atlas-data` JSON block and `atlas-data.json` identical when updating evidence. Change the displayed snapshot revision/date only after refreshing the corresponding source and records. The site is published by the existing Pages workflow; its sidebar links directly to the full-window HTML.

Before publication, verify all four views, evidence selections, desktop/mobile layout and these relative download links in the built site. Source inspection and artifact checks do not establish deployed security enforcement.
