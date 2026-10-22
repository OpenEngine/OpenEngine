---
title: OpenEngine docs
sidebar_label: Overview
description: OpenEngine runs every change through a graph you define, on the Claude Code and Codex subscriptions you already have.
---

OpenEngine runs every change through a graph you define, on the Claude Code and
Codex subscriptions you already have, and hands you a reviewed pull request.

New here? [Start with `oe go`](getting-started.md) to make your first local
change, connect GitHub, and work through PR reviews and fixes.

## Quickstart

On macOS or Linux, with Git and Node.js 20.19+ installed and Codex or Claude
logged in on this machine, run:

```sh
npm i -g @openengine.cc/engine
cd your-repo
engine go "add a dark mode toggle to the settings page"
```

The first `engine` command installs a pinned OpenEngine release under
`~/.local/share/openengine` and starts the local daemon at
[http://127.0.0.1:4364](http://127.0.0.1:4364). `engine go` runs the default
graph against the checkout you are in and follows it live.

Without npm, the one-line installer does the same and also puts an `engine`
launcher in `~/.local/bin` (add it to your `PATH` if `engine` is not found):

```sh
curl -LsSf https://openengine.cc/install.sh | sh
```

Use one method or the other: npm will not overwrite a launcher the curl
installer created, so if you switch to npm later, run
`npm i -g --force @openengine.cc/engine` once.

Run `engine daemon` to reopen the daemon, `engine daemon doctor` to diagnose
problems, and reinstall (either way) to upgrade.

## Concepts

<div className="cards">
  <a href="/docs/graphs/"><strong>Graphs</strong><span>How a change gets made, as a versioned graph.</span></a>
  <a href="/docs/loops/"><strong>Loops</strong><span>Run a graph on a cadence, within limits.</span></a>
  <a href="/docs/cli-reference/"><strong>CLI reference</strong><span>Every oe command and option.</span></a>
  <a href="/docs/integrations/"><strong>Integrations</strong><span>Slack, GitHub and MCP clients.</span></a>
</div>

## Repository trust

On the first interactive `oe go` or `oe graph run` in a checkout, Engine
asks whether agents may edit files and run commands without asking. Both yes and
no are remembered as `trusted = true` or `trusted = false` in the daemon's
`[repos."owner/name"]` table. Subfolders and other worktrees share that decision.

Use `oe repo trust [path]` or `oe repo untrust [path]` to change it later.
Run commands also accept `--trust` and `--no-trust`. Non-interactive and `--json`
runs never prompt; use an explicit flag to persist a decision. Onboarding accepts
`--trust` / `--no-trust`, and web and MCP onboarding offer the same choice.

`[approvals] auto_approve = false` remains the global default. Setting it to true
skips the trust prompt and auto-approves across repositories; repository trust
opts in individual repositories while that global setting stays false. Explicit
false means manual approvals and no repeat prompt. Unattended project runs remain
subject to their own manual approval policy. Tool allow rules are unchanged.

## Add a repository

Use `oe repo add .` (or `oe repo add /path/to/repo`) to register an
existing clone without cloning it again. The checkout must have a GitHub or
GitLab origin. To clone and register a remote repository, use:

```sh
oe repo add https://github.com/owner/repo
oe repo add https://gitlab.com/group/repo --path ~/code/repo
```

HTTPS and SSH URLs retain their transport and host. The default clone location
is `[onboarding] directory` or `~/code`, followed by the repository's final name.
Local paths and clone destinations belong to the selected daemon; with a local
backend, relative paths resolve from your current directory. Connected mode is
the default; use `--mode disconnected` for local-only work. Trust can be set with
`--trust` or `--no-trust`. `oe repo add` replaces `engine onboard`.
