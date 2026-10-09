---
slug: orchestrate-graphs-and-loops
title: How to use OpenEngine to orchestrate graphs and loops
authors: [sheahawkins]
tags: [openengine, graphs, loops, code-review]
---

Graphs and loops are the two core primitives of OpenEngine: a graph is a repeatable agent workflow, such as an adversarial code review, and a loop runs one on a schedule.

<!-- truncate -->

The post walks through composing graphs, then builds a dead-code removal graph that scans for unused code, challenges each finding, and removes what survives, on a loop.

**[Read it on Substack →](https://sheahawkins.substack.com/p/how-to-use-openengine-to-orchestrate)**

For the details, see [Graphs](/docs/graphs/), [Loops](/docs/loops/) and the [CLI reference](/docs/cli-reference/).
