# Database schema history

Each revision document contains the database schema after applying that Alembic
revision and a highlighted SQL diff against each parent (including both parents
of a merge). Data-only and merge revisions are retained even when they make no
schema changes. Alembic's bookkeeping table and SQLite internal objects are omitted.

The schema documentation workflow runs on migration changes in pushes and PRs.
It connects to disposable SQLite databases and a PostgreSQL service, replays
every revision, and commits changed documents to the source branch. It posts
each changed schema and its diff to open PRs for that branch. Large documents
are linked when they exceed GitHub's comment limit. Generated commits do not
touch the workflow's watched paths, preventing a commit loop.

Fork PRs receive an artifact instead of commits or comments because their token
is read-only. Branch rules must permit the GitHub Actions token to commit for
automatic publication; normal branch protection is not bypassed. A stale run
does not overwrite newer branch commits. Artifacts are available on every run.

Regenerate SQLite documentation locally:

```sh
uv run python scripts/document_schema.py
```

Include PostgreSQL with `--postgres-url postgresql+psycopg://...` and an installed
`psycopg` driver. Use an **empty disposable database**, never a deployed database:
the generator executes migration code. PostgreSQL revisions run in rolled-back
transactions; their documents list reflected tables, constraints, and indexes.
The PostgreSQL history currently reserves an empty initial schema. SQLite
documents include tables, indexes, views, and triggers from `sqlite_schema`.

Documents use revision IDs under `sqlite/`, `sqlite_graph/`, and `postgres/`.
Regeneration is deterministic and preserves archived documents if a revision is
removed. LangGraph's separately managed checkpoint schema is not an Alembic history.
