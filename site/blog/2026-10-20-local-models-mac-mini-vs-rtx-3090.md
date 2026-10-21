---
slug: local-models-mac-mini-vs-rtx-3090
title: Running Local Models for Agents - a Mac mini vs a 6-Year-Old GPU
authors: [dethstrobe]
tags: [local-models, ollama, opencode, benchmarks, openengine]
---

We wanted OpenEngine's implementation agents to run on a local model, so we benchmarked the two machines we had: a Mac mini with an M5 Pro and 64 GB of memory, and a Linux box with an RTX 3090, a GPU from 2020.

**The 3090 won.** With the same model file on the same server software, the Mac decoded at about 16 tokens a second against the 3090's 41, and on a real agent task it took 3.1x as long (3 h 7 min of `implement` against 1 h). Our first comparison made the Mac look close, but the two machines weren't running the same files or software. Once we matched them, the gap was much bigger.

<!-- truncate -->

## The setup

| | Mac mini | Linux box |
|---|---|---|
| Hardware | Apple M5 Pro, 64 GB unified memory | NVIDIA GeForce RTX 3090 |
| Model | `Qwen3.8-27B-UD-Q4_K_XL.gguf` (27.8B dense, 17.6 GB) | The same file; the SHA-256 is identical |
| Server | Homebrew llama.cpp 0.7.0 (build 11669, Metal) | llama.cpp build 11151 (CUDA), in a container |
| Flags | `-ngl 999 -fa on --cache-type-k q8_0 --cache-type-v q8_0 --parallel 1 --jinja -c 131072` | The same |
| Agent limits | 65,536-token context, 8,192 output | The same |
| Reached over | localhost | Tailscale |

The builds differ on purpose: we left each machine's own. The Mac has the newer one and still came out well behind, so the build gap doesn't explain the result.

## The benchmark

We measured 256-token completions (median of 3) and prompts of about 8,300 tokens (2 runs, each starting with a random nonce so a prefix cache can't help).

| | Decode | Prefill (about 8.3k tokens) |
|---|---|---|
| Mac mini, llama.cpp | 15.6 tok/s | 322 to 339 tok/s |
| Mac mini, Ollama's MLX runner (`nvfp4`) | 29.6 tok/s | 428 to 429 tok/s |
| *RTX 3090* | *40.9 tok/s* | *1,150 to 1,210 tok/s* |

- **On the same weights and software, the 3090 is about 2.6x faster at decode and 3.5x faster at prefill.**
- **Quantizing the KV cache isn't the cause.** An f16 cache gave the Mac the same decode and prefill.
- **MLX decodes about 1.9x faster than llama.cpp on the same Mac.** But it runs a different set of weights (`nvfp4`), so it isn't a like-for-like result. The model file has a multi-token-prediction head that llama.cpp logs as unused. We guessed that MLX uses it. llama.cpp can too, with `--spec-type draft-mtp`, and that lifted the Mac's decode from 15.6 to about 25.6 tok/s (see [Making the Mac faster](#making-the-mac-faster)), so the guess looks right.

Caveats: the 3090 row comes from an earlier script and prompt, and the Mac's Ollama figures moved between runs (42.6 tok/s once, 29.6 later), so treat the ratios as rough.

## A real task on the matched setup

The task is [#952](https://github.com/OpenEngine/OpenEngine/issues/952): show run step summaries as Markdown in the web UI. It's frontend work and it needs a new npm dependency.

OpenEngine's `tdd-guide` graph works a task test-first, in the red-green-refactor order. One agent writes failing tests (red), and a fresh session makes them pass without changing them (green). The change then goes to a pull request and a review panel, and the panel's findings go back to the implementer, who revises the code with the tests still passing. That review loop is the refactor step. A person approves the result at the end.

Here, Claude wrote the failing tests once with a separate `tdd-tests` graph and pushed them to a branch. `tdd-guide` then ran twice from that commit, once with the Linux box as the implementer and once with the Mac, started within four seconds of each other. Each run opened a pull request and went through the review panel, with up to three `implement` attempts. Both used all three.

| `implement` step | RTX 3090 | Mac mini |
|---|---|---|
| Pull request | [#954](https://github.com/OpenEngine/OpenEngine/pull/954) | [#956](https://github.com/OpenEngine/OpenEngine/pull/956) |
| **Total** | **59 min 51 s** | **3 h 7 min** |
| First attempt | 15 min 54 s | 1 h 41 min |
| Model steps | 150 | 164 |
| Output tokens | 98.0k | 117.2k |
| Output tokens per second | 27.3 | 10.4 |
| Time per model step | 24 s | 69 s |
| Prompt tokens newly prefilled, per step | about 2.1k | about 2.7k |

Step counts and tokens come from each server's own log, because OpenEngine doesn't report token usage for local models yet ([#812](https://github.com/OpenEngine/OpenEngine/issues/812)).

- **The Mac took 3.1x as long.** That matches the benchmark: 2.6x on decode and 3.5x on prefill. Over the whole step the Mac wrote 10.4 tokens a second against 27.3, so the rate of writing mostly decides the total time.
- **The Mac also did more work.** It made 9% more steps and wrote 20% more output tokens, almost all of it in the first attempt (73 steps against 33). With one run each, we can't say whether that's the quantization, the runtime or chance.
- **Prefix caching does its job.** Steps carry tens of thousands of tokens of history, yet each prefilled only about 2,100 (3090) and 2,700 (Mac) new tokens. The cold-prompt prefill gap barely matters here.

### The code

Both pull requests take the same approach. Both add `react-markdown` and `remark-breaks`, render the summary through a new `Markdown` component, let only `http`, `https` and `mailto` links open (in a new tab, with `rel="noopener noreferrer"`), and render no images. Real CI is green on both. We read both diffs, not the tests, and we didn't run either branch.

| | RTX 3090 (#954) | Mac mini (#956) |
|---|---|---|
| `markdown.tsx` | 59 lines, self-contained | 42 lines, imports `linkable()` from `github-activity` |
| Link safety | A new `safeHref` that parses the URL | Reuses `linkable()`, with a new `mailto` boolean argument |
| Bulleted and numbered lists | **Show no markers**: the global `list-style: none` is never undone | Show `disc` and `decimal` |
| Headings in a summary | Every level at body size | Unstyled: `##` takes the step title's 19px |
| Review findings at the end | 1 medium, 5 low | 4 low |

- **The 3090's component is the better encapsulated.** It depends on nothing else in the app. The Mac's imports from the GitHub-activity module, which is the wrong direction for a generic component, and it widens a shared guard with a boolean flag, which also makes that guard case-insensitive for GitHub comment links.
- **The Mac's is smaller and its CSS is right.** It reuses the existing guard, and its lists render. The 3090's missing list markers are the one functional bug either pull request has. It's visible, no test catches it, and it survived all three review rounds.
- **Neither is clearly better.** The same weights produced two reasonable, differently flawed changes. If we had to pick one to merge, we'd lean slightly to the Mac's because its flaw is cosmetic and it's less code to review.

Caveats: one task and one run per machine. The step that consolidates the reviewers' findings and the one that opens the pull request ran on each run's local model, a poor choice since consolidating is a judgment call, and we've since moved both to Claude. Counting every step up to the human approval, the 3090's run took 1 h 31 min and the Mac's 4 h 16 min (2.8x).

## What we saw before matching the setup

Our first two tasks ran the Mac on Ollama's MLX runner and the 3090 on llama.cpp, with different files. The numbers mix hardware, quantization and serving software, so read them as history.

**The first benchmark** made the Mac look close: decode was a tie (42.6 against 40.9 tok/s) and the 3090 was 2.9x faster at prefill. A mixture-of-experts model, Qwen3-Coder (30B, about 3B active parameters), roughly doubled the Mac's decode to 81.8 tok/s and brought its prefill within 20% of the 3090's. That one ran on a different runner, so some of the difference may be the runner. It was fast, but as an agent it wasn't reliable enough: see the refactor below.

**A small refactor** (#776, an explicit `DaemonState` enum) ran the same 27.8B model on both machines. The 3090 finished its `implement` step in 38 min against the Mac's 53 min, 1.4x faster, with fewer steps (59 against 68) and fewer output tokens (55.7k against 71.9k). The two pull requests ([#906](https://github.com/OpenEngine/OpenEngine/pull/906), [#907](https://github.com/OpenEngine/OpenEngine/pull/907)) were nearly the same change. The 1.4x gap shrank because the Mac was on the faster MLX runner, not because the hardware was close.

We also ran this refactor twice on Qwen3-Coder, the mixture-of-experts model, on the Mac. Its first `implement` step took 5 min 15 s and the second 9 min 16 s, against 38 to 54 min for the dense model, but neither result was usable. The first ([#919](https://github.com/OpenEngine/OpenEngine/pull/919)) passed its own state tests but failed three of the four security reproducer tests: a 401, 403 or 502 reply carrying an OpenEngine body was reported as `ready` and should have been `foreign`. The dense model's code passes the same tests, and the MoE run's second pass didn't fix it. The second ([#920](https://github.com/OpenEngine/OpenEngine/pull/920)) doesn't import: it uses `DaemonState` in a module-level mapping before the enum is defined, so every daemon and connect test errors at collection, and it failed at the publish step before any reviewer saw it. Two samples don't prove much, but one that works partly and one that doesn't import is not an agent we could rely on, so we stopped using it.

**A small feature** (#813, show the health of a local model's endpoint in the web UI) used one set of tests for every run, written by a new `tdd-tests` graph. We ran it on the 3090 and twice on the Mac, with and without a context limit.

| `implement`, total | RTX 3090 (#922) | Mac, no limit (#923) | Mac, limit set (#932) |
|---|---|---|---|
| Time | 2 h 52 min | 6 h 56 min | 2 h 50 min |
| Peak context | 67k | 161k | 63k |

- **Only the 3090 delivered the feature end to end.** It is the only one whose health readings both get collected in a real daemon and appear somewhere a user can see them. Both Mac runs left the new agents list unmounted, and the limited run left the poller off by default. The shared tests passed all three anyway, because they render the component alone and start the poller by hand. That's a gap in the tests as much as in the models ([#939](https://github.com/OpenEngine/OpenEngine/issues/939)).
- **None finished clean on security.** Every run hit the three-attempt limit with a reproducer still failing, and all three were rated Red. The 3090 logs a URL's credentials every 30 seconds while an endpoint is down. The Mac's first run (#923) is the only one that guards the address: about 150 lines resolve each host, refuse the cloud metadata block, and pin the connection to the checked address. We didn't re-run its reproducers on its final commit. It was merged into the shared tests branch and reverted in #942.
- **The limited Mac run has a bug that nothing flagged.** An unexpected error from one endpoint can abort the whole poll pass and record no reading for any agent. We read this in the code and didn't run it.
- **CI didn't check any of this.** OpenEngine's CI step reported all three as passing within seconds, without waiting for CI to finish, so none of the runs got real CI feedback.

## Cap the context window

The biggest speed-up came from one setting: telling opencode how big the model's context window is. On the Mac it cut the first `implement` attempt from 3 h 36 min to 1 h 15 min.

| First `implement` attempt, same machine, model and tests | No limit | Limit set |
|---|---|---|
| Peak context | 161k | 59k |
| Time per model step | 2 min 0 s | 45 s |
| Time for the attempt | 3 h 36 min | 1 h 15 min |

Set the window in two places, to the same value: on the server, and in opencode as `limit.context` on the model so it knows when to compact. Ollama's OpenAI-compatible endpoint can't set the window per request, so its side is `PARAMETER num_ctx 65536` in the model's Modelfile. llama.cpp takes `-c`.

OpenEngine runs a local model by registering it as an agent with `engine agent add`. An agent is a name that graphs can use as a runner, such as `implementer=mac-gguf` in `tdd-guide`. For a local model you add an `opencode` agent and give it `--url`, the address of a server that speaks the OpenAI API, which both llama.cpp's `llama-server` and Ollama do. The context cap is an option on the same command ([#935](https://github.com/OpenEngine/OpenEngine/issues/935), shipped in #937):

```bash
engine agent add opencode --name mac-gguf --model qwen3.8 \
  --url http://127.0.0.1:8000/v1 --context 65536 --output 8192
```

- `--name` is what graphs call the agent.
- `--model` is the model name the server knows it by.
- `--url` points the agent at your server instead of a hosted model.
- `--context` is the window opencode compacts before. Match it to the server's window, or to a smaller cap.
- `--output` is the most tokens one reply may use.

`--context` is the one that matters. Without it, opencode doesn't know how big the model's window is and never compacts, so a long task keeps adding to every step's prompt. Nothing fails when that happens. The run finishes, only about **three times slower**, which is what we saw on the Mac: prompts of up to 161k tokens sent to a 65,536-token window, and a first attempt that took 3 h 36 min instead of 1 h 15 min. A local model's slowest job is reading a long prompt, so this is the setting that decides whether an agent on one is usable. Add it whenever you add a local agent with `--url`.

## Why a 2020 GPU wins

Split the work in two.

- **Decode** is limited by memory bandwidth, because each new token reads the model's weights.
- **Prefill** processes many tokens at once and is limited by raw compute. A desktop GPU has far more of it than an Apple SoC.

Unified memory's advantage is capacity, not speed. The Mac holds a large model easily, but it isn't faster at chewing through a long prompt.

That matters most for agents. Before every step the model re-reads the whole conversation: the system prompt, tool definitions, every file it opened. Prefix caching softens this once the session is warm. When it isn't, a prompt of 210,000 tokens takes about 8 minutes to prefill on the Mac and about 3 on the 3090, and an agent on a local model can look hung while it reads. We saw gaps of up to 17 minutes between steps on the Mac.

## Making the Mac faster

Nothing we tried closes the decode gap, but multi-token prediction narrows it from 2.6x to about 1.6x, and some other things moved the numbers too.

**What didn't**

- **A faster, smaller model.** Qwen3-Coder, a mixture-of-experts model with about 3B active parameters, decoded at 81.8 tok/s on the Mac, twice the dense model, and its prefill came within 20% of the 3090's. As an agent it missed requirements and once produced code that didn't import ([#919](https://github.com/OpenEngine/OpenEngine/pull/919), [#920](https://github.com/OpenEngine/OpenEngine/pull/920)), so the speed wasn't worth it.
- **A different KV cache type.** Switching from q8_0 to f16 changed neither decode nor prefill.
- **A newer llama.cpp build.** The Mac already had the newer one (build 11669 against the Linux box's 11151) and is still 2.6x slower at decode, so a newer build alone doesn't close the gap.

**What helped**

- **Cap the context window.** The one big win we measured: the first attempt went from 3 h 36 min to 1 h 15 min, because opencode compacts its history instead of carrying 161k tokens. See [Cap the context window](#cap-the-context-window).
- **Turn on multi-token prediction in llama.cpp.** The model file carries a prediction head that llama.cpp ignores by default. Starting `llama-server` with `--spec-type draft-mtp` makes it draft a few tokens ahead and keep the ones the full model agrees with. In a quick test with the same flags otherwise, decode went from 15.6 to a median of 25.6 tok/s on a prose prompt (runs of 21 to 26) and from 15.6 to 22.8 on a coding prompt. About half the drafted tokens were accepted. Prefill didn't change (326 to 334 tok/s against 343). Two caveats: the output at temperature 0 wasn't identical to the plain server's on the coding prompt, so check quality before relying on it, and we haven't run it on an agent task. We also haven't tried it on the 3090, which may gain too.
- **Use the MLX runner.** MLX decoded about 1.9x faster than llama.cpp on the same machine (29.6 against 15.6 tok/s). The weights are a different quantization (`nvfp4`), whose quality we haven't compared with the GGUF.
- **Use the local model only for `implement`.** We moved the steps that judge the review and open the pull request to Claude. Consolidating the review ran at 12 tokens a second on the Mac, and its output decides what the next attempt works on.

## Conclusion

For a local agent, the 3090 is the better machine. On the same model file and server it was 2.6x faster at decode and 3.5x faster at prefill, and a real task took the Mac 3.1x as long. The machine changed how long the work took, not what the model wrote. The model did: Qwen3-Coder was fast but unreliable.

If you only have a Mac, it works for tasks you can leave running. Cap the context window first, then try multi-token prediction. This is one task per machine, so treat the ratios as a guide.
