# `[BETA]` WorkOrders, in plain English

Status: implemented for `apps/web`

There are now two kinds of workflow you can pick when you create a WorkOrder.
This page explains what the second kind is, what happens when you pick it, and
what it cannot do yet — no background in the codebase assumed.

## The two kinds

Every workflow lives as a file in the `workflows` directory, and that directory
is what this deployment knows how to run.

- **A step workflow** is a list: do this, then that, then ask a person. The
  part of OpenEngine that has been running for months reads the list and works
  through it. These appear in the dropdown with a version next to them, like
  `Implementation review · v1`.
- **A graph workflow** is a drawing: boxes with arrows between them. A
  different engine — LangGraph — runs those. These appear in the dropdown with
  `[BETA]` in front of their name and no version.

Both do roughly the same job for the implementation-review workflow: make a
checkout, let an agent change the code, let an agent review the change, then
stop and wait for a person to say yes or no. They differ in what the engine
underneath can do, which is the reason the second one exists:

- The checkout is one of the boxes. If the checkout fails, the run stops
  *there*, visibly, instead of the whole thing failing before it ever started.
- Waiting for a person does not end the agent's turn. It sits there, holding
  the conversation, and carries on when you answer — so answering is a reply
  rather than a fresh start.
- The same is true when an agent asks permission mid-task.

## What happens when you pick one

1. You choose a `[BETA]` entry, type your task and repository, and press
   create.
2. The web server hands the task and the repository to the graph engine and
   asks it to start that graph. The agent is not a separate choice here: there
   is one `[BETA]` entry per agent (`(codex)`, `(claude)`), so picking the
   entry is picking the agent.
3. The graph engine gives the run an id, and the WorkOrder you see is saved
   under that same id — so both halves are talking about the same run.
4. You land on the WorkOrder page, which shows the task, the repository and
   whether the run is still going.

## What it cannot do yet — and this is why it says `[BETA]`

The WorkOrder page cannot show you a graph run's stages, its agents'
conversations, or the question it stopped on. It shows a row and an ending.

Everything else is served by the graph engine's own API, which the web server
passes through under `/graph`:

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

A run stops and waits the first time an agent asks permission, and again at the
end when it wants a person's verdict. Until the pages catch up, those are
answered with the last call above. `GET /graph/api/runs/{run}` lists what is
outstanding, with the id to answer.

## Where things are kept

- The graph engine writes what it knows into two small database files under
  `graph-state/` next to where you started the server (`graph_state_directory`
  in `apps/web/.../composition.py`). Delete that folder and the beta runs are
  forgotten; the WorkOrder rows in `conversations.sqlite3` would remain.
- Because the graph engine keeps its own record, a restart does not lose a run
  that was waiting for you. It picks the run back up when your answer arrives,
  rather than when the server starts.
- The step executor is told to leave graph WorkOrders alone on startup. It
  would otherwise try to resume one and look for a list of steps that a graph
  does not have.

## If no `[BETA]` entries appear

Then this deployment's `workflows` directory holds no graph workflows, so no
graph engine was started and there is nothing to offer. That is deliberate: an
entry nobody could start is worse than no entry at all.
