# OpenEngine

**Define your software development lifecycle as a graph. Run every change through it on the Claude Code and Codex subscriptions you already have. Get back a reviewed pull request.**

[![GitHub stars](https://img.shields.io/github/stars/OpenEngine/OpenEngine?style=flat&logo=github&label=stars)](https://github.com/OpenEngine/OpenEngine/stargazers)
[![npm](https://img.shields.io/npm/v/%40openengine.cc%2Fengine?logo=npm&label=npm)](https://www.npmjs.com/package/@openengine.cc/engine)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![Coverage baseline: 85.12%](https://img.shields.io/badge/coverage%20baseline-85.12%25-brightgreen)](https://github.com/OpenEngine/OpenEngine/actions/workflows/tests.yml)
[![Docs](https://img.shields.io/badge/docs-openengine.cc-blue)](https://openengine.cc/docs/)
[![Slack: join the community](https://img.shields.io/badge/Slack-join%20the%20community-4A154B?logo=slack)](https://join.slack.com/t/openenginegroup/shared_invite/zt-49mkaebkz-m86SbPAwn_QNMPqsSgioYQ)

Coding agents are fast. The hard part is making their output trustworthy every time, not just when you are watching. OpenEngine is an open-source software factory: you write your lifecycle once as a graph (implement, wait for CI, fan out adversarial reviewers, rerank the findings, fix, open the PR), and OpenEngine runs every change through it, on demand or on a schedule. Add a security reviewer to the graph once and it checks every change from then on.

![OpenEngine lifecycle graph: implement, run CI, review in parallel, rerank, and open a pull request](docs/images/oe_land.png)

https://github.com/user-attachments/assets/f76429d7-86b4-4a37-af2d-52124af453c0

## Try it in two minutes

You need macOS or Linux, Git, Node.js 20.19+, and [Claude Code](https://docs.anthropic.com/en/docs/claude-code) or [Codex](https://github.com/openai/codex) logged in on this machine.

```bash
npm i -g @openengine.cc/engine
cd your-repo
oe graph run implementation-review-rerank "add a dark mode toggle to the settings page"
```

That runs the built-in implementation-review-rerank graph: implement, check CI, review the change from several angles, fix what the review finds, and open a pull request. You follow the run live in your terminal and can steer it mid-flight. The first time you run it in a repository, it asks whether to trust it; trusted repositories run without stopping for approvals.

Prefer not to install? `npx @openengine.cc/engine@latest graph run implementation-review-rerank "..."` works too. The [getting started guide](https://openengine.cc/docs/getting-started/) walks through your first local run, connecting GitHub, and reviewing and fixing existing pull requests.

## What you can do with it

**Review pull requests you already have.**
```bash
oe graph run review --input pr_url=https://github.com/OWNER/REPO/pull/123   # post findings on a PR
```

**Write your own graph** in YAML or Python. Graphs are versioned, so a change to the process is as reviewable as a change to the code.
```bash
oe graphs                                   # list available graphs
oe graph add my-graph.yaml                  # register your own
oe graph run my-graph "add my widget"       # run it with an instruction
```
The [example graphs](docs/examples/graphs) include TDD, dead-code removal, and the [security review](docs/examples/graphs/security-review.yaml) that OpenEngine runs on itself every day.

**Steer a run while it works.**
```bash
oe run watch <run-id>
oe node steer <execution-id> "use the existing date helper instead"
```

**Put a graph on a schedule** with loops: QA sweeps, dependency upkeep, nightly security review. Each loop has limits on spend and pull requests, so it cannot run away with your budget.
```bash
oe loop add qa-sweep --every 1d --max-spend 20 --max-prs 5
oe loops                                    # status, spend and PRs per loop
```

**Start work from where you already are.** Mention OpenEngine in a Slack thread or a PR comment to start, steer, or approve a run. See [integrations](https://openengine.cc/docs/integrations/).

## How it works

A **graph** is a sequence of agent and code steps with typed inputs and outputs. A **run** is one execution of a graph, managed by a local background **daemon** (`oe daemon`). Agents are the coding CLIs you already use: the built-in `claude`, `codex`, and `opencode`, plus any you add, including local models through Ollama or any OpenAI-compatible server. Review steps can require a different agent than the one that wrote the code, which is what makes the review adversarial.

The default graph that builds OpenEngine itself:
```
implement -> CI -> [security, bugs, performance, conciseness, dryness] -> rerank -> impact -> human review
```
Findings go back for one fix-and-review round before a human sees the PR. The source is [`workflows/implementation_review_graph.py`](./workflows/implementation_review_graph.py); the full architecture is in [ARCHITECTURE.md](ARCHITECTURE.md).

## Learn more

- [Docs](https://openengine.cc/docs/) — getting started, graphs, loops, CLI reference, integrations
- [Blog](https://openengine.cc/blog/) — how we use OpenEngine, benchmarks, and what the loops find
- [CLI reference](cli/README.md) — every `oe` command
- [Connecting GitHub and GitLab](docs/forge-connections.md) — OAuth setup, webhooks, and server deployments
- [Developing OpenEngine](docs/development.md) — running from source, the dev server, configuration, and migrations
- [CHANGELOG](CHANGELOG.md) and [releases](https://github.com/OpenEngine/OpenEngine/releases)

## Community

Questions, ideas, or a graph you are proud of? Join us on [Slack](https://join.slack.com/t/openenginegroup/shared_invite/zt-49mkaebkz-m86SbPAwn_QNMPqsSgioYQ) or open a [discussion](https://github.com/OpenEngine/OpenEngine/issues). Contributions are welcome; [CONTRIBUTING.md](CONTRIBUTING.md) explains how the repository is organized and how to get a change merged. Issues labeled [`good first issue`](https://github.com/OpenEngine/OpenEngine/labels/good%20first%20issue) are a good place to start.

If OpenEngine is useful to you, a star on GitHub helps other people find it.

## License

[Apache-2.0](LICENSE).
