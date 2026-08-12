Core concepts
Engine

The engine is the pure decision-making kernel.

It receives:

State + Event

and produces:

New State + Commands

It does not call GitHub, Temporal, Codex, a database, or the filesystem.

Workflow

A workflow describes which events cause which actions.

For example:

on(TaskReady).run_agent("coder")

on(AgentCompleted).run_agent(
    "correctness-reviewer",
    "architecture-reviewer",
    "impact-reviewer",
)

on(ClarificationRequested).ask_human()

on(ClarificationAnswered).run_agent("coder")

Workflow definitions are pure configuration understood by the engine.

They do not contain Temporal-specific behavior.

Commands

Commands describe an intention to affect the outside world.

Examples:

RunAgent
CreateWorkspace
OpenChangeRequest
RunReviews
AskHuman
DestroyWorkspace

The engine emits commands but never executes them.

Events

Events describe something that has happened.

Examples:

TaskReady
AgentCompleted
AgentFailed
ClarificationRequested
ClarificationAnswered
ChangeOpened
ChangeUpdated
ReviewsPassed
ReviewsFailed

Events are fed into the engine by the workflow runtime.

Runtime / Command Dispatcher

The runtime connects the pure engine to the outside world.

It receives an engine command such as:

RunAgent(profile="coder")

and determines which configured capability should execute it.

RunAgent
   ↓
CommandDispatcher
   ↓
AgentRouter
   ↓
AgentRunner port
   ↓
Codex adapter

The runtime owns execution mechanics; the engine owns decisions.

Ports

Ports describe capabilities the system requires without specifying implementations.

Workflow Runtime

Executes workflows durably or locally and delivers events back to the engine.

Initial implementations:

LocalWorkflowRuntime
TemporalWorkflowRuntime
Store

Persists application state.

Initial implementations:

SQLiteStore
PostgresStore

The Store contains things such as tasks, agents, conversations, messages, runs, reviews, and workspace metadata.

Agent Runner

Executes a configured agent.

Implementations might include:

Codex CLI
Claude Code CLI
Other coding CLIs
Source Control

Provides repository/change-management capabilities.

Initial implementation:

GitHub

Potential future implementation:

GitLab
Communications

Allows the system to send messages or clarifying questions to humans.

Initial implementation can be the web control interface.

Later:

Buzz
Slack
other channels

Incoming replies become engine events rather than calls directly into an agent.

Workspace Provider

Creates and manages environments where agents can work with source code.

Initial implementation:

Local Git worktrees

Future implementations could use containers, Kubernetes, VMs, or remote sandboxes.

Agent Identity

There are three levels of agent identity.

Agent
  ↓
AgentInstance
  ↓
AgentRun
Agent

The logical role.

Examples:

planner
coder
security-reviewer
architecture-reviewer
impact-reviewer
AgentInstance

A persistent logical instance of an agent.

It owns continuity such as:

identity
conversation
task association

It does not belong to Codex or Claude.

A provider-specific session may be associated with an AgentInstance, but that session is an adapter optimization rather than the canonical identity.

AgentRun

One execution of an AgentInstance.

AgentInstance agi_123
    │
    ├── AgentRun run_1
    ├── AgentRun run_2
    └── AgentRun run_3

This allows an agent to stop for clarification and later continue as the same logical instance.

Conversation Model

Conversation history is owned by the Store, not by the coding CLI.

AgentInstance
      │
      ▼
Conversation
      │
      ├── Message
      ├── Message
      ├── Message
      └── Message

The adapter may maintain a native Codex/Claude session for efficiency, but our stored conversation remains the source of truth.

Agent execution history is separate:

Conversation / Messages
    = what was said

AgentRun / RunEvents
    = what the agent did
Workspace Model

A workspace belongs to the work, not to an AgentInstance.

Task ENG-42
     │
     ▼
Workspace ws_42
     │
     ├── coder run 1
     ├── coder asks clarification
     ├── coder run 2
     └── coder run 3

Repeated runs working on the same task reuse the same writable workspace.

For local execution, the initial implementation is a Git worktree:

repository
   │
   ├── main checkout
   │
   ├── worktree ENG-42
   ├── worktree ENG-43
   └── worktree ENG-44

Two agents should not concurrently modify the same writable workspace.

A workspace's work outlives its checkout. The directory is expendable -- removed, swept out of /tmp, lost to a reboot -- while what was done in it is not, so the two are separable:

Workspace ws_42
     │
     ├── work     = branch engine/ws_42        durable, always checkoutable
     └── checkout = worktree on that branch    attach / detach at will

Detaching removes the checkout and keeps the branch, snapshotting uncommitted work onto it first; attaching checks the branch out again under the same workspace id, so nothing holding that id has to be told. Disposing is the one that ends both.

Reviewers should operate against immutable/read-only views of a specific commit:

coding workspace
      │
      ▼
   commit abc123
      │
      ├── correctness reviewer
      ├── security reviewer
      └── architecture reviewer
Clarification Flow

Agents can pause work and ask humans for additional information.

Coder
  │
  │ ClarificationRequested
  ▼
Engine
  │
  │ AskHuman
  ▼
Runtime
  │
  ▼
Communications Port
  │
  ├── Web UI
  └── Buzz / Slack
          │
          ▼
        Human
          │
          │ reply
          ▼
ClarificationAnswered
          │
          ▼
Workflow Runtime
          │
          ▼
Engine
          │
          │ RunAgent
          ▼
same AgentInstance
same Workspace

The communications adapter does not directly wake or invoke the coding agent.

It turns human communication into an event that re-enters the workflow.

Example Coding Workflow
TaskReady
    │
    ▼
CreateWorkspace
    │
    ▼
RunAgent(coder)
    │
    ├──────────── ClarificationRequested
    │                        │
    │                        ▼
    │                    AskHuman
    │                        │
    │               ClarificationAnswered
    │                        │
    └────────────────────────┘
    │
    ▼
AgentCompleted
    │
    ▼
OpenChangeRequest
    │
    ▼
GitHub PR
    │
    ▼
Impact Analysis
    │
    ├──────────────┬─────────────────┐
    ▼              ▼                 ▼
Correctness    Architecture       Security
Reviewer        Reviewer          Reviewer
    │              │                 │
    └──────────────┴────────┬────────┘
                            ▼
                      Review Result
                       │          │
                       │          │
                 changes needed   passed
                       │          │
                       ▼          ▼
                 RunAgent(coder) Merge Ready
                       │
                       └── same workspace
Dependency Rule

The most important architectural constraint is:

Domain / Engine / Workflow Definitions
             │
             │ know nothing about
             ▼

Temporal
GitHub
Codex
Claude
Buzz
SQLite
Postgres
Docker
HTTP

External implementations depend inward on our abstractions.

                 CORE

        Domain
          ▲
          │
      Engine / Workflows

        ─────────────
          boundary
        ─────────────

        Ports / Runtime
              ▲
              │
           Adapters
              ▲
              │
   Temporal / GitHub / Codex / etc.

The engine decides what should happen.

The runtime determines how to make it happen.

Ports define what capabilities are available.

Adapters define how those capabilities are implemented.