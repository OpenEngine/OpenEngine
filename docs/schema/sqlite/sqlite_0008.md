# sqlite: sqlite_0008

Remove workstreams, rehoming their runs on the milestone above them.

## Changes from sqlite_0007

```diff
--- sqlite_0007
+++ sqlite_0008
@@ -6,11 +6,7 @@

 CREATE INDEX runs_by_milestone ON run_states (milestone_id);

-CREATE INDEX runs_by_workstream ON run_states (workstream_id);
-
 CREATE INDEX session_grants_by_instance ON session_grants (instance_id);
-
-CREATE INDEX workstreams_by_milestone ON workstreams (milestone_id);

 CREATE TABLE agent_instances (
    sequence INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
@@ -102,11 +98,9 @@
    sequence INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    state_json TEXT NOT NULL,
-   workstream_id TEXT,
    milestone_id TEXT,
    PRIMARY KEY (sequence),
    CONSTRAINT runs_milestone_fk FOREIGN KEY(milestone_id) REFERENCES milestones (milestone_id),
-   FOREIGN KEY(workstream_id) REFERENCES workstreams (workstream_id),
    UNIQUE (run_id)
 );

@@ -124,12 +118,3 @@
    FOREIGN KEY(instance_id) REFERENCES agent_instances (instance_id),
    UNIQUE (grant_id)
 );
-
-CREATE TABLE workstreams (
-   sequence INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
-   workstream_id TEXT NOT NULL,
-   milestone_id TEXT NOT NULL,
-   name TEXT NOT NULL, scope TEXT DEFAULT '' NOT NULL,
-   FOREIGN KEY(milestone_id) REFERENCES milestones (milestone_id),
-   UNIQUE (workstream_id)
-);
```

## Schema

```sql
CREATE INDEX approvals_by_run ON approvals (agent_run_id);

CREATE INDEX messages_by_instance ON messages (instance_id, sequence);

CREATE INDEX milestones_by_project ON milestones (project_id);

CREATE INDEX runs_by_milestone ON run_states (milestone_id);

CREATE INDEX session_grants_by_instance ON session_grants (instance_id);

CREATE TABLE agent_instances (
    sequence INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    instance_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    task_id TEXT,
    workspace_id TEXT,
    title TEXT DEFAULT 'New chat' NOT NULL,
    archived INTEGER DEFAULT '0' NOT NULL,
    runner TEXT DEFAULT '' NOT NULL,
    auto_approve INTEGER DEFAULT '0' NOT NULL,
    workflow_run_id TEXT,
    workflow_step_id TEXT,
    UNIQUE (instance_id),
    UNIQUE (conversation_id)
);

CREATE TABLE agent_runs (
    agent_run_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    status TEXT NOT NULL,
    summary TEXT NOT NULL,
    changed_files TEXT NOT NULL,
    runner TEXT NOT NULL,
    PRIMARY KEY (agent_run_id),
    FOREIGN KEY(instance_id) REFERENCES agent_instances (instance_id)
);

CREATE TABLE approvals (
    sequence INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    approval_id TEXT NOT NULL,
    agent_run_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    runner TEXT NOT NULL,
    kind TEXT NOT NULL,
    reason TEXT,
    command TEXT,
    cwd TEXT,
    tool_name TEXT,
    tool_call_id TEXT,
    workspace_id TEXT,
    arguments TEXT,
    questions TEXT,
    answers TEXT,
    allowed_decisions TEXT NOT NULL,
    status TEXT NOT NULL,
    decision TEXT,
    decision_source TEXT,
    requested_at TEXT NOT NULL,
    decided_at TEXT,
    FOREIGN KEY(instance_id) REFERENCES agent_instances (instance_id),
    UNIQUE (approval_id)
);

CREATE TABLE messages (
    sequence INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    instance_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tool_calls TEXT NOT NULL,
    tool_call_id TEXT,
    FOREIGN KEY(instance_id) REFERENCES agent_instances (instance_id)
);

CREATE TABLE milestones (
    sequence INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    milestone_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL, description TEXT DEFAULT '' NOT NULL, dependencies TEXT DEFAULT '[]' NOT NULL,
    FOREIGN KEY(project_id) REFERENCES projects (project_id),
    UNIQUE (milestone_id)
);

CREATE TABLE projects (
    sequence INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL, archived INTEGER DEFAULT '0' NOT NULL,
    UNIQUE (project_id)
);

CREATE TABLE run_events (
    sequence INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    event_json TEXT NOT NULL
);

CREATE TABLE "run_states" (
    sequence INTEGER NOT NULL,
    run_id TEXT NOT NULL,
    state_json TEXT NOT NULL,
    milestone_id TEXT,
    PRIMARY KEY (sequence),
    CONSTRAINT runs_milestone_fk FOREIGN KEY(milestone_id) REFERENCES milestones (milestone_id),
    UNIQUE (run_id)
);

CREATE TABLE session_grants (
    sequence INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    grant_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    runner TEXT NOT NULL,
    approval_kind TEXT NOT NULL,
    normalized_scope TEXT NOT NULL,
    workspace_id TEXT,
    created_from_approval_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    FOREIGN KEY(instance_id) REFERENCES agent_instances (instance_id),
    UNIQUE (grant_id)
);
```
