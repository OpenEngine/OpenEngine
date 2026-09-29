---
title: OpenEngine docs
sidebar_label: Overview
description: OpenEngine runs every change through a graph you define, on the coding CLIs you already pay for.
---

OpenEngine runs every change through a graph you define, on the coding CLIs you already pay for.

## Quickstart

Install OpenEngine on macOS or Linux (x86_64 or arm64). You need `curl`, `tar`,
and a SHA-256 tool (`shasum` or `sha256sum`).

To run coding agents, install Git and Node.js 20.19+ (including `npx`), and log
in to Codex or Claude on this machine. OpenEngine launches the ACP adapters
using your provider credentials.

Then install OpenEngine:

```sh
curl -LsSf https://openengine.sh/install.sh | sh
```

The installer downloads the latest release with its own Python and uv, installs
the `engine` command, starts OpenEngine in the background, and opens
[http://127.0.0.1:4364](http://127.0.0.1:4364) in your browser. No source checkout
or frontend build is needed.

By default, the command is installed in `~/.local/bin`. If that directory is
not on your `PATH`, add this line to your shell profile and reload it:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

Check the service and its runtime tools, or reopen the browser:

```sh
engine daemon status
engine daemon doctor
engine daemon
```

For an installation that does not start the service or open a browser, run:

```sh
curl -LsSf https://openengine.sh/install.sh | sh -s -- --no-start
```

Rerun the installer to upgrade. It preserves your configuration and conversation
state.

## Concepts

<div className="cards">
  <a href="/docs/workflows/"><strong>Workflows</strong><span>Your SDLC as a LangGraph graph.</span></a>
  <a href="/docs/workorders/"><strong>WorkOrders</strong><span>One task, run through a workflow.</span></a>
  <a href="/docs/integrations/"><strong>Integrations</strong><span>Slack and GitHub, in both directions.</span></a>
</div>
