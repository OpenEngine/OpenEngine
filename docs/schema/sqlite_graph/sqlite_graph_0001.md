# sqlite_graph: sqlite_graph_0001

Adopt graph runtime schema

## Changes from base

```diff
--- base
+++ sqlite_graph_0001
@@ -0,0 +1,37 @@
+CREATE INDEX approvals_by_run ON approvals (run_id);
+
+CREATE INDEX events_by_run ON events (run_id, sequence);
+
+CREATE TABLE approvals (
+   approval_id TEXT NOT NULL,
+   run_id TEXT NOT NULL,
+   record TEXT NOT NULL,
+   status TEXT DEFAULT 'pending' NOT NULL,
+   decision TEXT,
+   ordinal INTEGER,
+   PRIMARY KEY (approval_id)
+);
+
+CREATE TABLE events (
+   sequence INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
+   run_id TEXT NOT NULL,
+   kind TEXT NOT NULL,
+   payload TEXT NOT NULL,
+   node_id TEXT,
+   execution_id TEXT
+);
+
+CREATE TABLE runs (
+   run_id TEXT NOT NULL,
+   graph_id TEXT NOT NULL,
+   error TEXT DEFAULT '' NOT NULL,
+   ordinal INTEGER, auto_approve_nodes TEXT DEFAULT '[]' NOT NULL,
+   PRIMARY KEY (run_id)
+);
+
+CREATE TABLE sessions (
+   run_id TEXT NOT NULL,
+   session_key TEXT NOT NULL,
+   continuation TEXT NOT NULL,
+   PRIMARY KEY (run_id, session_key)
+);
```

## Schema

```sql
CREATE INDEX approvals_by_run ON approvals (run_id);

CREATE INDEX events_by_run ON events (run_id, sequence);

CREATE TABLE approvals (
    approval_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    record TEXT NOT NULL,
    status TEXT DEFAULT 'pending' NOT NULL,
    decision TEXT,
    ordinal INTEGER,
    PRIMARY KEY (approval_id)
);

CREATE TABLE events (
    sequence INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    node_id TEXT,
    execution_id TEXT
);

CREATE TABLE runs (
    run_id TEXT NOT NULL,
    graph_id TEXT NOT NULL,
    error TEXT DEFAULT '' NOT NULL,
    ordinal INTEGER, auto_approve_nodes TEXT DEFAULT '[]' NOT NULL,
    PRIMARY KEY (run_id)
);

CREATE TABLE sessions (
    run_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    continuation TEXT NOT NULL,
    PRIMARY KEY (run_id, session_key)
);
```
