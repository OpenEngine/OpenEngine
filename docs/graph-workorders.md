# Graph WorkOrders, in plain English

Status: implemented for `apps/web`

This page explains what a graph WorkOrder is, what happens when you create one,
and what it cannot do yet — no background in the codebase assumed.

## The two kinds

Every workflow lives as a file in the `workflows` directory, and that directory
is what this deployment knows how to run.

- **A step workflow** is a list: do this, then that, then ask a person. The
  part of OpenEngine that has been running for months reads the list and works
  through it. These appear in the dropdown with a version next to them, like
  `Implementation review · v1`.
- **A graph workflow** is a drawing: boxes with arrows between them. A
  different engine — LangGraph — runs those. These appear in the dropdown under
  their own name and with no version.

This repository ships one workflow and it is a graph. The step executor is
still shipped, so a deployment that installs a step definition of its own gets
both kinds in the same dropdown; nothing here installs one.

Both kinds do roughly the same job for the implementation-review workflow: make
a checkout, let an agent change the code, let an agent review the change, then
stop and wait for a person to say yes or no. They differ in what the engine
underneath can do, which is the reason the graph one exists:

- The checkout is one of the boxes. If the checkout fails, the run stops
  *there*, visibly, instead of the whole thing failing before it ever started.
- Waiting for a person does not end the agent's turn. It sits there, holding
  the conversation, and carries on when you answer — so answering is a reply
  rather than a fresh start.
- The same is true when an agent asks permission mid-task.

## What happens when you pick one

1. You choose a graph entry, type your task and repository, and expand
   **Workflow inputs** to fill in any declared fields. Implementation workflows
   offer independent **Implementation runner** and **Review runner** dropdowns:
   choose Codex or Claude for either stage, including the same runner for both.
   The workflow is `implementation-review-rerank`, with Codex implementation
   and Claude review by default. It is also the configured Slack workflow.
2. The web server validates the inputs and hands them, the task, and the
   repository to the graph engine. Naming and reranking use the implementation
   runner; all review facets use the review runner and its corresponding models.
3. The graph engine gives the run an id, and the WorkOrder you see is saved
   under that same id — so both halves are talking about the same run.
4. You land on the WorkOrder page, which shows the task, the repository and
   whether the run is still going.

## Following one, and talking to it

The rail lists a graph WorkOrder's conversations under its name —
**Implementation** and **Review** — from the moment the run exists, because
those are its graph's nodes rather than something that has to happen first. The
checkout and the human verdict are stages of the run rather than conversations
in it, so they are not offered there; a node says which it is with
`show_in_sidebar`.

The WorkOrder page shows a graph run's stages, and each agent node has an **Open
conversation** link once it has said anything. That conversation is the same
view a chat is: the task the node was given as the first turn, what the agent
said, its tool calls folded into rows you can open, and — while the agent is
working — a box to write in. What you write is *steering*: a message into the
turn the agent is in the middle of, not a new one. A node that has finished has
nothing in flight to say it to, so the box is not offered.

When an agent stops to ask permission, the request appears in that conversation
under the command it is about, with the buttons to answer it. The run's final
human verdict is answered from the WorkOrder page itself, in the **Action
required** panel.

## What it cannot do yet

The event log a conversation is drawn from lives in the server's memory, so
restarting the server empties it: the run picks back up (see below), but what
was said before the restart is gone from the page. Nothing else keeps a graph
run's transcript, so this is the one thing to know before relying on it.

A question the run is stopped on is not lost with it — approvals are stored, not
remembered — so after a restart the conversation still shows what is waiting on
you, above the message box, with nothing above it to explain what led there.

Everything the pages read is served by the graph engine's own API, which the web
server passes through under `/graph`:

```
GET  /graph/api/graphs                              every graph it can run
GET  /graph/api/runs/{run}                          where a run is now, and what
                                                    it is waiting for
GET  /graph/api/runs/{run}/events                   a live feed of everything the
                                                    run says
POST /graph/api/runs/{run}/steering                 send a message to whichever
                                                    agent is working
POST /graph/api/runs/{run}/approvals/{approval}     answer a question it stopped
                                                    on: {"decision": "accept"}
```

To follow one from a terminal, copy its run id from the WorkOrder page and tail
that event feed (the development server uses port 8000 by default):

```bash
curl -N http://localhost:8000/graph/api/runs/RUN_ID/events
```

The useful boundary events are `node.started` (LangGraph scheduled the step),
`conversation.started` (the ACP adapter connected and established the agent
session), `transcript` and `tool.call` (the agent is producing work), and
`approval.requested` (it is waiting for a decision). If a run fails, the
terminal running `engine-web` or the `[api]` side of `engine-dev` carries the
full Python traceback. Recent ACP adapter stderr is included there when the
adapter refuses a request or exits; it is otherwise kept out of the transcript.

A run stops and waits the first time an agent asks permission, and again at the
end when it wants a person's verdict. Both are answerable from the pages; the
last call above is the same answer given from a script, and
`GET /graph/api/runs/{run}` lists what is outstanding with the id to answer.

## Where things are kept

- The graph engine writes what it knows into two small database files under
  `graph-state/` next to where you started the server (`graph_state_directory`
  in `apps/web/.../composition.py`). Delete that folder and the graph runs are
  forgotten; the WorkOrder rows in `conversations.sqlite3` would remain.
- The step executor is told to leave graph WorkOrders alone on startup. It
  would otherwise try to resume one and look for a list of steps that a graph
  does not have.

## What a restart does to a run

Where a run got to is written down; the thing actually *working* through the
graph is not — it is a task inside the server process, and stopping the server
ends it. So when the server starts, it goes through every unfinished graph
WorkOrder and does one of three things:

| What the engine says about the run | What happens |
| --- | --- |
| It was working | It is sent back to the last position it saved, and carries on from there. Whatever the agent did after that position is lost — the process died mid-sentence, and there is no record of the rest. |
| It is waiting for you | Nothing. Your answer is what starts it again, and that works whether or not the server was restarted in between. |
| It finished or failed while the server was down | The WorkOrder row catches up to that ending, which it could not hear at the time. |

A run the engine has no record of at all — you deleted `graph-state/`, say — is
marked failed with that as the reason, rather than left claiming to be working
forever.

## If something is wrong with the graph engine itself

Two different things can go wrong here, and the server treats them differently
on purpose.

**A graph that does not compile** — a workflow file describes something that is
not a graph, perhaps after a dependency upgrade changed what LangGraph accepts.
The server **does not start**. The log says which graph it was and what was
wrong with it:

```
graph workflow 'implementation-review-rerank' does not compile, so this server
will not start: Graph must have an entrypoint: add at least one edge from START
```

This is a file somebody has to fix, and nothing improves by starting without it:
a server that quietly dropped the graph would be running a deployment nobody
configured, and you would find out the first time someone picked the workflow.

**Anything else about the engine's files** — the `graph-state/` directory is not
writable, a checkpoint file is being held by another process. That is about this
machine rather than about any graph, so the graph engine simply does not run:
the error is logged, the rest of the application starts normally, and no
graph entries appear in the dropdown because nothing in this process could
run one.

## If no graph entries appear

Then this deployment's `workflows` directory holds no graph workflows, so no
graph engine was started and there is nothing to offer. That is deliberate: an
entry nobody could start is worse than no entry at all.

## Renaming a workflow

A WorkOrder remembers the id it was started under, and nothing rewrites it. So
changing a graph's `id` orphans every run made before the change: no graph to
describe it, no state to read, and a row that cannot be opened.

List the old ids in `previous_ids` instead of dropping them:

```python
graph_workflow(
    pipeline(...),
    id="implementation-review-rerank",
    name="Implementation review rerank",
    previous_ids=("implementation-review-codex", "implementation-review-claude"),
)
```

The engine then answers for those ids as well as the current one, so older
WorkOrders keep their stages, their state and their transcripts. New runs are
started under the current id only; a retired id another graph now claims is
ignored rather than allowed to shadow it.

A workflow that leaves the directory without being retired this way is not an
error. Its WorkOrders stay in the list and their transcripts stay readable --
served under `/api/runs/{run}/graph-events`, which is recorded against the run
rather than the graph -- and the WorkOrder page says the workflow is no longer
available instead of drawing stages it cannot read. An unfinished one is failed
with that reason on the next restart, because nothing can pick it back up.

## Declaring inputs in a workflow

Pass `inputs` to either spelling of `graph_workflow`. Each `WorkflowInput`
(imported from `engine.graph_runtime_langgraph`) declares a `name`, `label`,
optional `default`, `required` flag, and optional tuple of `choices`. Fields
with choices render as dropdowns; other fields accept text. For example:

```python
inputs=(WorkflowInput(
    "review_runner", "Review runner", default="claude", required=True,
    choices=("codex", "claude"),
),)
```

WorkOrder creation accepts an `inputs` object in `POST /api/runs`, applies
omitted defaults, and rejects unknown fields or invalid values before starting
execution. Nodes read these values from `state["inputs"]`; they persist with
normal graph checkpoints. Workflows without declarations keep their existing
creation behavior.

Runner inputs are resolved once when the runtime creates the run. Agent nodes
bind a creation field through `graph_node_runner_input`; its value initializes
that node's persisted runner override. The conversation Runner control displays
and edits the same override. Returning to the workflow's original runner clears
the override, and retry uses that runner even when the original creation input
was different. Nodes can implement `_for_runner` to configure models and MCP
bindings for the resolved runner, including approval recovery.
