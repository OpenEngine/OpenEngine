---
slug: test-the-real-thing
title: Why we test OpenEngine against the real thing, with no mocks
authors: [dethstrobe]
tags: [openengine, testing, benchmark]
---

Every piece of OpenEngine had tests. The CLI had tests, the daemon's auth had tests, the GitHub login had tests. Then we ran the whole thing the way a stranger would, from a clean machine, and `oe connect gh` failed with `oe: authentication required`.

No test had put those pieces together. None of them ran the CLI against a daemon protected by a service token, and that is exactly how the guide tells you to run it.

<!-- truncate -->

That is the argument for testing agent software against the real thing, and this post is about what that looked like for us and what it found. We are not finished. The benchmark described here runs end to end today, but there is more to build around it.

## What we run

The benchmark starts a fresh Docker container with nothing in it but git, Node, the GitHub CLI, an editor and the coding agents. Then it follows a guide, one terminal command at a time, as a new user would:

1. Clone OpenEngine's latest `main`, install it and start the daemon.
2. Protect it with a token, open a tunnel so GitHub can reach it, and register a webhook.
3. Connect GitHub and point OpenEngine at an empty repository.
4. Copy the `tdd-guide` graph, trim it to fit a small project, and register it.
5. Start it on the first issue, then start a loop that works through the rest.

The repository has a milestone of eight GitHub issues: set up the project, release automatically with a changelog, write the README, then a calculator, one feature at a time. Everything on the other end is real. The agents are real Claude and Codex sessions. The pull requests are real, CI runs on real GitHub runners, and the calculator is deployed to real GitHub Pages.

The last full run took 54 minutes. It made ten runs, none failed, merged eight pull requests, cut releases by itself and left a calculator at [rock-ai-bot.github.io/oe-bench](https://rock-ai-bot.github.io/oe-bench/) that you can open and use.

The same run produces the documentation. The guide is generated from the test, so a step in the guide is a step that just ran. A guide nobody has followed is a guess about what works. This one has been followed, on every run.

## What the real run found

**A bug no unit test would have found.** The service token worked on some routes and not others. The `oe connect` commands call routes it was not accepted on, so a user who did everything the guide said hit a wall. We filed it as [#1249](https://github.com/OpenEngine/OpenEngine/issues/1249) and it was fixed in [#1250](https://github.com/OpenEngine/OpenEngine/pull/1250). Each side was correct about what it did. The failure was in the join between them.

**A rule that was right and a path that was long.** The daemon refuses to let the general service token mark a repository as trusted, because trust lets agents run commands without asking. That is a sensible rule. But walking the real path showed what it costs: a second token, a second backend and an extra step before the first run. A unit test of the rule would have passed. Only someone following the guide notices that the path to a safe default is long, and whether to shorten it is now a question we are asking.

**Other people's systems are part of your product.** GitHub's list of webhook deliveries lagged by minutes, so a delivery that had arrived looked as if it never did. Our CI step could report that checks had passed before the pull request's workflow had even started. GitHub returned a 504 in the middle of a half-hour run and the harness treated it as fatal. None of these are bugs in our code, and all of them are things a user of ours will meet. A mock of GitHub contains only the behaviour its author already knew about. We now retry reads and not writes, because a merge that timed out may have happened.

**The model's choices are not something you can fake.** The calculator the agents built looked up the dot key by looping over every button on the page. It worked, and a person would have put an id on each button. We did not edit it, because the choice is the model's to make. We took it as a finding about our reviewer: it should have said something. We have drafted a change that has the reviewer read the whole file, count the copies of a pattern the change repeats and name the shared form, with tests for it. It is not merged yet, and we have not yet replayed it on the calculator's pull requests, so we do not know how much it helps. A stubbed agent would never have written that code, so there would have been nothing to review.

**Cost is its own finding.** The baseline run, fifteen issues in eighteen runs, took about three and a half hours, including a ninety-minute pause when we hit a Claude usage limit. The daemon estimated its cost at $1.57. On a subscription that figure means little: the number that matters is how much of the subscription is left, and we read that from the providers' own meters. We cut the milestone to eight issues, with a first issue that sets up the project, to make runs cheaper and faster.

## Who grades the run

When a system grades its own output, the grade is suspect. OpenEngine's agents write their own tests, so their tests passing proves little about whether the calculator works.

So the verdict comes from outside. After the journey passes, a fixed check that a person wrote, and that the agents never see, opens the deployed calculator and presses keys: `1+1=` must show `2`, `10÷0=` must show `Error` and then carry on, `C` must clear. It finds keys by their button names and the display by its visible text, with no ids, classes or test ids, so it does not care how the agents built the page. It reads as the contract: if it looks like a calculator to a person using it, it passes.

It runs as its own step, outside the guide's run. The check is not part of the guide. It exists to say whether the guide's run worked.

We should be honest about its limits. We have seen it fail a deliberately broken addition and pass a real calculator, but we have not built the suite of broken fixtures that would prove it catches every case.

## What it costs

Real testing is not free, and we would rather say so than pretend.

- **Time.** A run takes close to an hour, so it cannot run on every pull request. It runs on demand. We have an [open issue](https://github.com/OpenEngine/OpenEngine/issues/1263) to run it on a schedule.
- **Money.** It spends real subscription budget, and a usage limit can stop it partway.
- **Flakiness from outside.** GitHub is not always fast or available. A run can fail for reasons that have nothing to do with us. The harness has to tell those apart from real failures, and it does so imperfectly.
- **State.** It uses one repository on one account, and the next run deletes it, so only one run can go at a time.

We keep fast tests too. Most of our end-to-end tests use stubbed agents and run on every change, and they catch a lot. The real run is not a replacement. It answers the question the stubs cannot: what happens when all the real parts meet.

## What we would tell someone starting

- **Start from a clean machine and the first command a user would type.** Everything you forgot you set up on your own computer shows up there.
- **Let the test produce the docs.** One run gives you a verdict and a guide, and the guide cannot go out of date without the test failing.
- **Keep the grader outside the thing being graded.**
- **Measure what the run costs, and mistrust estimates.** Look at the real meters.
- **Expect the first findings to be about the joins.** Our first real failure was not in any component. It was in how they fit.

Next on our list are checks that the calculator documents itself and releases itself, then repeated runs so we can compare one setup against another, and running it on a schedule. The guide itself is generated from the latest passing run.
