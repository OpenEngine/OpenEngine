# Native implementation-review graph milestones

This plan breaks [issue #179](https://github.com/spiralsoft-ai/OpenEngine/issues/179)
into milestones that retire architectural risk before the native LangGraph graph
becomes a production path. An issue belongs to the earliest milestone that needs
its answer; later milestones depend on the completion of every earlier one.

## Native graph architecture approved

**Exit criterion:** the team has a documented go/no-go decision for a native
LangGraph implementation-review workflow and an agreed configuration boundary.

- [x] [#180](https://github.com/spiralsoft-ai/OpenEngine/issues/180) — Prove the
  implementation-to-review happy path with `ACPNode`.
- [x] [#181](https://github.com/spiralsoft-ai/OpenEngine/issues/181) — Decide how
  OpenEngine consumes the standalone `langgraph-acp` package.
- [ ] [#193](https://github.com/spiralsoft-ai/OpenEngine/issues/193) — Compare the
  LangGraph approach with extending the current workflow DSL and make the
  go/no-go recommendation.
- [ ] [#206](https://github.com/spiralsoft-ai/OpenEngine/issues/206) — Define the
  user-facing SDLC configuration surface so the graph is built against the
  intended product boundary.

## Agent execution contracts established

**Depends on:** Native graph architecture approved.

**Exit criterion:** an `ACPNode` workflow can run with the required tools. Agent
outputs and workflow naming have explicit contracts, and rejection behavior is
intentional.

- [ ] [#182](https://github.com/spiralsoft-ai/OpenEngine/issues/182) — Expose the
  existing MCP-server path to graph-run agents.
- [ ] [#195](https://github.com/spiralsoft-ai/OpenEngine/issues/195) — Define and
  validate typed step output.
- [ ] [#196](https://github.com/spiralsoft-ai/OpenEngine/issues/196) — Preserve
  terminal rejection or explicitly specify a bounded rework loop.
- [ ] [#197](https://github.com/spiralsoft-ai/OpenEngine/issues/197) — Preserve or
  deliberately replace workflow auto-naming.

## Durable human review and recovery

**Depends on:** Agent execution contracts established.

**Exit criterion:** graph state is persisted and exposed, human review can pause
and resume safely, and crash recovery cannot duplicate external effects.

- [ ] [#183](https://github.com/spiralsoft-ai/OpenEngine/issues/183) — Represent
  human review as a durable interrupt and surface it in the web UI.
- [ ] [#184](https://github.com/spiralsoft-ai/OpenEngine/issues/184) — Choose the
  graph checkpointer/state-store integration and read model.
- [ ] [#198](https://github.com/spiralsoft-ai/OpenEngine/issues/198) — Guarantee
  effectively-once external side effects across crash recovery.

Issues #183 and #184 should be designed together: the interrupt's durable state
and the UI projection must agree on ownership and resume semantics. Issue #198
must be resolved before any graph node is allowed to publish GitHub changes.

## Native implementation-review workflow

**Depends on:** Durable human review and recovery.

**Exit criterion:** the repository-owned implementation-review workflow runs as
a selectable native LangGraph graph with implementation, review, and human-review
steps, while the existing DSL remains available to other workflows. Only reranked
critical findings can publish review comments.

- [ ] [#194](https://github.com/spiralsoft-ai/OpenEngine/issues/194) — Move comment
  authority after reranking and prove that only zero to three selected findings
  are posted.
- [ ] [#185](https://github.com/spiralsoft-ai/OpenEngine/issues/185) — Build and
  wire the production implementation-review graph.

## Production parity verified

**Depends on:** Native implementation-review workflow.

**Exit criterion:** an end-to-end test covers real worktrees, fake agent CLIs,
durable persistence, tool access, transitions, and the human-review gate, and
demonstrates the agreed behavior of the workflow.

- [ ] [#186](https://github.com/spiralsoft-ai/OpenEngine/issues/186) — Add the
  end-to-end parity and acceptance test.

Closing #186 completes the acceptance bar for #179. Any deliberate departure
from the current workflow must be recorded in the relevant contract issue before
the parity test is updated to assert it.
