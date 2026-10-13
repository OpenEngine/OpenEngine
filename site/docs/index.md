---
title: OpenEngine docs
sidebar_label: Overview
description: OpenEngine runs every change through a graph you define, on the coding CLIs you already pay for.
---

OpenEngine runs every change through a graph you define, on the coding CLIs you already pay for.

## Quickstart

On macOS or Linux, with Git and Node.js 20.19+ installed and Codex or Claude
logged in on this machine:

```sh
npm i -g @openengine.cc/engine
engine --help
```

Install and upgrade with `npm i -g @openengine.cc/engine`. Run `engine daemon setup`
to configure and start the local service. Coming from the curl installer, use
`npm i -g --force @openengine.cc/engine` once to replace its launcher.

The curl installer remains an alternative:

```sh
curl -LsSf https://openengine.cc/install.sh | sh
```

Run `engine daemon doctor` to diagnose service problems.

## Concepts

<div className="cards">
  <a href="/docs/graphs/"><strong>Graphs</strong><span>How a change gets made, as a versioned graph.</span></a>
  <a href="/docs/loops/"><strong>Loops</strong><span>Run a graph on a cadence, within limits.</span></a>
  <a href="/docs/cli-reference/"><strong>CLI reference</strong><span>Every engine command and option.</span></a>
  <a href="/docs/integrations/"><strong>Integrations</strong><span>Slack, GitHub and MCP clients.</span></a>
</div>

## Repository trust

On the first interactive `engine go` or `engine graph run` in a checkout, Engine
asks whether agents may edit files and run commands without asking. Both yes and
no are remembered in the daemon's machine config under `[trusted_repos]`, using
a name from `[repos]`. Subfolders and other worktrees share that decision.

Use `engine repo trust [path]` or `engine repo untrust [path]` to change it later.
Run commands also accept `--trust` and `--no-trust`. Non-interactive and `--json`
runs never prompt; use an explicit flag to persist a decision. Onboarding accepts
`--trust` / `--no-trust`, and web and MCP onboarding offer the same choice.

`[approvals] auto_approve = false` remains the global default. Setting it to true
skips the trust prompt and auto-approves across repositories; repository trust
opts in individual repositories while that global setting stays false. Explicit
false means manual approvals and no repeat prompt. Unattended project runs remain
subject to their own manual approval policy. Tool allow rules are unchanged.

## Add a repository

Use `engine repo add .` (or `engine repo add /path/to/repo`) to register an
existing clone without cloning it again. The checkout must have a GitHub or
GitLab origin. To clone and register a remote repository, use:

```sh
engine repo add https://github.com/owner/repo
engine repo add https://gitlab.com/group/repo --path ~/code/repo
```

HTTPS and SSH URLs retain their transport and host. The default clone location
is `[onboarding] directory` or `~/code`, followed by the repository's final name.
Local paths and clone destinations belong to the selected daemon; with a local
backend, relative paths resolve from your current directory. Connected mode is
the default; use `--mode disconnected` for local-only work. Trust can be set with
`--trust` or `--no-trust`. `engine repo add` replaces `engine onboard`.
