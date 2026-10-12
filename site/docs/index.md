---
title: OpenEngine docs
sidebar_label: Overview
description: OpenEngine runs every change through a graph you define, on the coding CLIs you already pay for.
---

OpenEngine runs every change through a graph you define, on the coding CLIs you already pay for.

## Quickstart

On macOS or Linux, with Git and Node.js 20.19+ installed and Codex or Claude
logged in on this machine, run:

```sh
curl -LsSf https://openengine.cc/install.sh | sh
```

This installs the `engine` command in `~/.local/bin`, starts OpenEngine, and
opens [http://127.0.0.1:4364](http://127.0.0.1:4364). If `engine` is not found
afterwards, add `~/.local/bin` to your `PATH`.

Run `engine daemon` to reopen it, `engine daemon doctor` to diagnose problems,
and rerun the installer to upgrade.

## Concepts

<div className="cards">
  <a href="/docs/graphs/"><strong>Graphs</strong><span>How a change gets made, as a versioned graph.</span></a>
  <a href="/docs/loops/"><strong>Loops</strong><span>Run a graph on a cadence, within limits.</span></a>
  <a href="/docs/cli-reference/"><strong>CLI reference</strong><span>Every engine command and option.</span></a>
  <a href="/docs/integrations/"><strong>Integrations</strong><span>Slack, GitHub and MCP clients.</span></a>
</div>
