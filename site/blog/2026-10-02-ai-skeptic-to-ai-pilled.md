---
slug: ai-skeptic-to-ai-pilled
title: In One Year I Went from AI Skeptic to AI Pilled
authors: [dethstrobe]
tags: [ai, agentic-coding, claude-code, code-review, openengine]
---

Last year (in 2025), I left my job at little known tech giant, Google, and looked to build software that I thought mattered.

Back then (in 2025), I used a bit of GitHub Copilot and found their output lacking. So for the most part I wrote most of my code by hand. Earlier this year, I had a friend tell me that Claude Code changed everything, so I started to use that, and it produced very good code, very consistently.

I slowly, over months changed my stance on coding by hand.

<!-- truncate -->

## Forced to Agentic code

So, back in March of this year (2026) I got hit by a car and broke my collarbone. It was my own fault, so no shade to the driver. But, being down one arm for a couple of months, did slow down how much I could code by hand.

Back in January, Claude Code came out in v1, and I ignored it. I just didn't believe that agents could do as good of a job as me. But with a broken bone and limited mobility while I waited for surgery, I decided to take the plunge and get a Claude subscription.

My productivity tripled, even with a broken arm. I was able to implement features 3 times faster in my SaaS product I was making at the time. It's [ScheduleLord](https://www.schedulelord.com/) in case you're curious.

And it wrote code, and tests, and generated docs, just as good as me. Not exactly like me, I still needed to do a lot of steering, but the speed of implementation and confidence in code was very high.

## How We Decided to Build a Software Factory

So, while working on ScheduleLord I met Shea at our co-working space in Englewood, CO. He talked to me about how to automate the SDLC (software development life cycle), and how we can make that part faster so we can make the stuff we want faster and better.

After about a month of discussion, he convinced me to join him to build...

## OpenEngine: Automate Software Development Life Cycle

So enters [OpenEngine](https://github.com/OpenEngine/OpenEngine). A software factory.

It is:
- **Open Source**: Take a look at it. Fork it. Improve it.
- **Meets developers where they're at**: We created integrations with GitHub and Slack. So we can prompt and steer from where we already hangout.
- **Use the subscriptions you already have**: So we are using the [ACP](https://agentclientprotocol.com/get-started/introduction) to connect to the harness you're already paying for.

The idea is to remove all the parts of software development that don't require human involvement.

And much to my surprise it works. Not only does it work, it now arguably writes better code than I do.

## How to raise code quality, speed, and become AI pilled

After you [install OpenEngine](https://openengine.cc/docs/), we have this concept called a workorder.

Think of it as giving it a ticket to work on.

Each workorder runs nodes, which are agents which have a specific task.

### Naming
This is a basic one. It will name the workorder so we can find it later.

The Naming node will also go fetch more data from a GitHub issue, for example, to gain more context on what the scope of work will be to help name the workorder.

### Implementation
The implementor node is where the majority of code happens. It will write the implementation and write tests to validate that the implementation is done.

### Reviewer
The Reviewer node, is actually 5 reviewer nodes, a Reranker, and Impact analysis, and human review step. This is where the secret sauce happens. After the reviewers run, their output is fed back into the implementor once to solve obvious gaps it missed.

#### The Reviewers
The 5 reviewers have different domains they're specifically looking for. So their context can be dedicated to that.

1. **Security**: This will find if any code changes are potential security risks.
2. **Bugs & task adherence**: This will find any bugs and make sure that the task is implemented as it was asked for.
3. **Performance**: Looks for ways to improve performance. Suggest caching, or make sure the implementation is optimal.
4. **Conciseness**: This will attempt to make sure the implementor didn't go overboard on the code and code comments.
5. **DRYness & code duplication**: This will attempt to make sure that the implementor is using library code, or create abstractions for code that is reused.

#### Reranker
The reranker's job is to take all the noise from the reviewers and make sure only the important stuff is surfaced during pull request code review. Then you as the human reviewer can decide if it's worth addressing or not.

#### Impact analysis
The impact analysis node reviews everything that is changed and will flag how much of an impact it is.

- **Green**: This means the code is likely not to impact anything major and should be safe to merge. We're testing this now, to see if we can trust that _Green_ PRs can be merged without human review. Give us some time to see how true that is.
- **Orange**: This means a change could potentially have dangerous side effects. There are often trade-offs that need to be made, and it is up to the human reviewer to decide if the trade-off is worth it.
- **Red**: This means it is likely a significant change, and possibly has security implications or breaking changes. So it needs a human review to make sure the trade-offs and changes are well understood before merging.

#### Human Review
The real last step in the Reviewer node is to get human approval for the change. You can view the pull request in GitHub and with the GitHub integration, once you approve and merge it, it gets marked as approved in OpenEngine.

### Extra eyes on the code make it better
So with all these extra review steps, it produces high code quality changes and provides much higher trust that the change was made correctly.

We're actually looking at doing even more with this, with evidence collection. Stay tuned for that. But the basic concept is we want to present evidence that the change is the intended functionality with screenshots, videos, a staging environment, and documentation. So that human review becomes more of a quality assurance that everything is behaving correctly without needing to download the code and verify it yourself.

## Real world examples

So while implementing the GitHub integration, we actually ran into a lot of permissions issues. We needed to make sure people had access to the GitHub repo and we implemented some caching so we didn't need to go to GitHub every time to recheck permission. Which also has security consequences, because someone whose GitHub access was revoked can keep using OpenEngine for up to 5 minutes. But this felt like an acceptable trade-off.

Permission checking was not at the top of my priority while implementing the integration, getting functionality was. So it was great that OpenEngine review process was looking out for these obvious footguns.

## Building faster with more confidence

So with all that, OpenEngine has made us faster. We're looking at trying to make a few pet projects to test it with iOS, Android, and Web development. But more feedback is always better.

So please give it a shot. Download and install it. And give us feedback on what we can do to improve it.

To give us that feedback, please join our [Slack](https://join.slack.com/t/openenginegroup/shared_invite/zt-4bwp1vipu-M4AE40owScSyoepfchvylA) to keep up with all the discussion and help inform our decisions on the product.