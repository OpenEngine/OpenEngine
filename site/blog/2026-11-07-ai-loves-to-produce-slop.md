---
slug: ai-loves-to-produce-slop
title: "AI Loves to Produce Slop: Giving Coding Agents an Understanding of Your Codebase"
authors: [sheahawkins]
tags: [openengine, codebox, code-analysis, agentic-coding]
---

AI loves to produce slop.

Don't get me wrong. AI coding agents are incredible. They can implement features, fix bugs, write tests, and open pull requests faster than ever.

But generating code quickly isn't the same thing as maintaining a healthy codebase.

<!-- truncate -->

We've been experiencing this firsthand while building [OpenEngine](https://github.com/OpenEngine/OpenEngine), an open-source platform for orchestrating AI coding agents.

One particularly painful example is a Python file called `api.py`.

At the time of writing, it's **4,776 lines long**.

That's a lot of code for one file. But the size isn't even the most frustrating part.

The problem is that `api.py` has become a hub for our web application. It handles HTTP endpoints, conversations, streaming responses, thread management, approvals, and more.

As we've had multiple AI agents working on different features, they keep finding reasons to modify this same file.

One agent adds an endpoint. Another changes how conversations work. A third modifies approval handling.

Suddenly, we're resolving merge conflicts between changes that should have been largely independent.

Each agent might be doing something perfectly reasonable in isolation. Collectively, they're making the codebase harder to maintain.

**The agents are optimizing for completing their tasks, not necessarily for the long-term health of the software.**

This got me thinking: what if we could give AI agents a better understanding of the architecture they're modifying?

Not just the files relevant to their immediate task, but the broader structure of the codebase, how it evolves, and where architectural problems are beginning to accumulate.

Software engineers have been studying these problems for decades. We already have tools for analyzing complexity, dependencies, coupling, and code churn.

The challenge is making that information accessible to coding agents.

That's why I started building [Codebox](https://github.com/SheaHawkins/codebox).

## Three Layers of Code Analysis

I've been thinking about code analysis in three layers:

- **Static analysis:** What does the codebase look like?
- **Historical analysis:** How is the codebase changing?
- **Agent-aware analysis:** How can agents use that information to make better decisions?

Codebox brings together several established analysis techniques into a single toolbox.

It's simultaneously a CLI, an MCP server, and an agent skill. That means we can use it to generate reports, but agents can also query the underlying information directly while working.

Let's walk through the techniques using OpenEngine's `api.py` as our example.

## 1. Static Analysis: Understanding What Exists

We know `api.py` contains 4,776 lines of code.

But file size alone isn't a particularly sophisticated metric.

A large file might contain hundreds of simple, independent functions. Another might contain a handful of deeply interconnected functions with complicated control flow.

Those are very different situations.

To understand the difference, we need to look at the structure of the code.

### Parsing Code with Tree-sitter

The first step is understanding what we're looking at.

[Tree-sitter](https://tree-sitter.github.io/tree-sitter/) is a parser generator and incremental parsing library that can construct syntax trees for source code.

Instead of treating a file as a collection of strings, we can identify its actual structure: functions, classes, imports, and call expressions.

Codebox uses Tree-sitter through `tree-sitter-language-pack` to extract structural information across hundreds of programming languages.

This gives us the foundation for answering questions like:

- What functions are defined in `api.py`?
- Which functions call other functions?
- Which modules does the file import?
- Where are particular symbols defined?

We can start exploring with:

```bash
codebox overview
codebox outline apps/web/src/engine/apps/web/api.py
```

The important thing is that we're no longer asking an agent to read thousands of lines and mentally reconstruct the architecture.

We're giving it a structured representation of the code.

### Building a Code Graph with LadybugDB

Once we've parsed the code, we need somewhere to store its relationships.

Codebox uses [LadybugDB](https://github.com/LadybugDB/ladybug), an embedded graph database.

The graph contains files, symbols, modules, and relationships such as `CALLS`, `CONTAINS`, and `IMPORTS`.

For example, a function might call three other functions, which themselves call several more.

Representing those relationships as a graph lets us explore how changes might propagate through the system.

We can ask Codebox:

```bash
codebox callers some_function --depth 2
codebox callees some_function
codebox impact some_function
```

These commands let us investigate callers, callees, and potential downstream impact.

For our `api.py` example, this is particularly useful.

We want to know which functions are central to the application and which parts of the system could be affected if we change them.

There's an important caveat: Codebox currently resolves calls primarily by name, using several heuristics. Dynamic dispatch and ambiguous references can limit accuracy.

So the graph is a useful architectural model, not a perfect representation of every runtime relationship.

Still, even an approximate graph can provide significantly more context than reading files individually.

### Measuring Complexity with Lizard

Next, we want to understand which parts of the code are difficult to reason about.

Codebox uses [Lizard](https://github.com/terryyin/lizard) to measure cyclomatic complexity.

Cyclomatic complexity approximates the number of independent paths through a function's control flow.

A function containing a handful of straightforward statements is generally easier to understand than one with deeply nested conditionals and numerous branches.

We can identify complex functions with:

```bash
codebox complexity --min-ccn 15
```

This returns functions meeting the specified complexity threshold.

```text
$ codebox complexity --min-ccn 15
apps/web/src/chat.tsx:86                           function=ToolElapsed  ccn=158  nloc=788  params=0
apps/open-verify/src/open_verify/runner.py:204     function=apply  ccn=79  nloc=186  params=2
cli/service/src/engine/graph_service/targets.py:37 function=resolve_target  ccn=71  nloc=76  params=6
apps/open-verify/src/open_verify/github.py:118     function=publish  ccn=59  nloc=148  params=3
```

It's probably not a coincidence that the files here are also the ones that have given us the most trouble. Now imagine combining this information with our code graph.

A function with high complexity might be concerning.

But a function with high complexity that also sits at the center of the call graph deserves considerably more attention.

This is where individual metrics start becoming useful architectural signals.

### Finding Patterns with ast-grep

Sometimes the problem isn't a particular function or dependency.

It's a pattern repeated throughout the codebase.

For that, Codebox integrates [ast-grep](https://ast-grep.github.io/).

Unlike traditional text search, ast-grep understands the syntactic structure of code.

For example:

```bash
codebox search 'eval($X)' -l python
```

This searches for calls matching the specified code pattern.

Structural searches are useful for identifying deprecated APIs, unsafe patterns, and opportunities to standardize implementations.

Codebox also includes Ripgrep for ordinary text searches and Semgrep for more sophisticated security and bug-detection rules.

These tools complement each other.

Tree-sitter helps us understand structure. LadybugDB captures relationships. Lizard measures complexity. ast-grep and Semgrep identify patterns that deserve investigation.

Together, they give us a much richer picture of the codebase.

## 2. Historical Analysis: Understanding How Code Changes

Static analysis tells us what the codebase looks like today.

But software isn't static.

It's constantly changing, and understanding that evolution is often more valuable than examining a single snapshot.

Consider two files.

One contains 2,000 lines of code but hasn't changed in six months.

Another contains 800 lines and has been modified 50 times in the last month.

Which one deserves more attention?

The answer depends on several factors, but change frequency is an important clue.

### Measuring Code Churn

Git provides a history of how our codebase evolves.

![Codebox hotspots chart ranking files by commits times complexity: api.py scores 168,756, roughly ten times the next file, service.py at 17,370](/img/blog/codebox-hotspots.png)

We can use it to understand which files change most frequently and how much code is added or removed.

Codebox combines Git history with complexity metrics to identify architectural hotspots.

For example:

```bash
codebox hotspots --days 90
```

This analyzes recent changes and identifies files where churn and complexity overlap.

```text
$ codebox hotspots --days 90
apps/web/src/engine/apps/web/api.py                commits=164  churn=10998  ccn=1029  score=168756
tests/test_web_app.py                              commits=121  churn=13929  ccn=395  score=47795
cli/service/src/engine/graph_service/service.py    commits=30  churn=2217  ccn=579  score=17370
apps/web/src/runs.tsx                              commits=63  churn=3502  ccn=259  score=16317
tests/test_graph_runtime_langgraph_acp.py          commits=37  churn=3181  ccn=330  score=12210
```

It's again no coincidence that `api.py` is top of the list.

We already know it's large and handles multiple responsibilities.

What we still need to measure is how frequently it changes, how its complexity has evolved, and whether it's one of the repository's most active hotspots.

Those measurements would help distinguish an architectural concern from a merely large file.

### Finding Hidden Coupling

Another interesting technique is examining which files tend to change together.

Suppose two modules have no obvious dependency relationship, but developers consistently modify both whenever they implement a feature.

That suggests some form of coupling that might not appear in an import graph.

This is often called *temporal coupling* or *change coupling*.

It can reveal architectural relationships that static analysis misses.

Codebox's report includes a file-coupling heatmap, making this kind of relationship easier to investigate.

![Codebox file-coupling heatmap counting how often pairs of files change together; graph_service/service.py and graph_service/store.py changed together 98 times](/img/blog/codebox-coupling-heatmap.png)

Those relationships might suggest natural boundaries for extracting functionality into separate modules.

However, files changing together doesn't automatically mean the architecture is wrong.

A feature might legitimately span multiple components.

As with the other metrics, the goal is to identify areas worth investigating rather than automatically declaring them problematic.

## 3. Agent-Aware Analysis: Making the Information Actionable

This is where things get particularly interesting.

Traditional code analysis tools were primarily designed to help humans understand software.

But AI coding agents create an opportunity to use that information much more actively.

Instead of generating a report that someone might read occasionally, we can make architectural information available to agents while they're implementing changes.

### Understanding the Impact Before Editing

Imagine an agent working on a new feature in OpenEngine.

Its initial plan involves adding another endpoint to `api.py`.

Before modifying the file, the agent queries Codebox.

It discovers that the relevant function has high cyclomatic complexity, several callers, and belongs to a file with substantial recent churn.

That information doesn't necessarily mean the agent should avoid modifying the file.

But it should influence the implementation strategy.

Perhaps the new functionality belongs in a separate module.

Perhaps an existing abstraction could be reused.

Perhaps the agent should make a smaller, more isolated change.

**The important distinction is that the agent now has architectural context before it starts writing code.**

### Understanding the Blast Radius

Suppose an agent needs to modify a function used throughout the application.

With a code graph, it can inspect the function's callers and identify potentially affected components.

Codebox exposes this through its impact-analysis command:

```bash
codebox impact some_function
```

This is useful both during implementation and review.

An agent can use the results to determine which parts of the system deserve additional testing.

It can also explain why a seemingly small change might carry more risk than expected.

### Detecting Architectural Drift

There's another opportunity here.

What if we ran code analysis before and after every pull request?

We could compare complexity, coupling, and other structural metrics.

Did the change introduce a highly complex function?

Did it increase dependencies between previously independent modules?

Did it add functionality to a file that's already an architectural hotspot?

These aren't necessarily reasons to reject a pull request.

Sometimes complexity is justified.

But they are useful questions for an automated reviewer to ask.

Over time, this could help us detect architectural drift before it becomes a major problem.

### Coordinating Multiple Agents

Finally, let's return to the problem that started this whole investigation.

Multiple agents modifying `api.py`.

Codebox can tell us about the structure and history of the file.

But OpenEngine has another source of information: the agents themselves.

Because OpenEngine orchestrates agent execution, it can potentially track which agents are working on which files.

Combining that execution information with Codebox's architectural analysis would let us identify contention.

Imagine detecting that three agents are about to modify the same high-churn module.

Rather than allowing all three to proceed independently, OpenEngine could coordinate their work.

It might serialize conflicting changes, adjust task boundaries, or ask an agent to reconsider its implementation strategy.

That's a future integration opportunity, rather than something Codebox currently guarantees.

But it illustrates why making code analysis accessible to agents is so interesting.

We're moving from passively measuring architectural problems toward actively preventing them.

## Bringing It Together with Codebox

One of the reasons I built Codebox was to make these techniques accessible without requiring developers to assemble and integrate half a dozen separate tools.

You can install it as a plugin for Claude Code or Codex, or use it directly from the command line.

For example:

```bash
uv tool install git+https://github.com/SheaHawkins/codebox
```

Then, from your repository:

```bash
codebox overview
codebox complexity --min-ccn 15
codebox hotspots --days 90
codebox report -o report.html
```

The HTML report brings together several perspectives on the codebase, including:

- Interactive call graphs and dependency layers.
- Complexity distributions and file-size visualizations.
- Architectural hotspots and coupling heatmaps.
- Potentially unreferenced code.
- Ranked recommendations supported by code evidence.

For agents, Codebox also exposes its capabilities through MCP and a dedicated skill.

That means an agent can query the code graph directly, investigate potential hotspots, and retrieve specific evidence without relying exclusively on a static report.

The broader goal is to make architectural understanding part of the normal development process.

## The Bigger Picture

AI coding agents are getting better at writing code.

But writing code is only one part of software engineering.

Maintaining a healthy codebase requires understanding dependencies, recognizing patterns, managing complexity, and making architectural tradeoffs.

These are problems we've been studying for decades.

We already have tools for measuring many of them.

What's exciting is the opportunity to combine those tools with agent orchestration.

If agents can generate code faster than humans, perhaps they can also help us continuously analyze and improve that code.

The goal isn't to stop agents from writing slop through increasingly elaborate prompts.

It's to give them the information they need to recognize architectural problems and make better decisions.

Because the real challenge isn't getting AI to write more code.

**It's getting AI to leave the codebase better than it found it.**

*This post was originally published on [Shea's Substack](https://sheahawkins.substack.com/p/ai-loves-to-produce-slop-giving-coding).*
