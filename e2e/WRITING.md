# Writing the guides

The guides on the site are user guides to the `oe` command. Each one is
generated from a spec in `tests/` (see the README), so these rules apply to
the spec: its describe titles, step titles and annotations are the page.

## The reader

Someone new to OpenEngine. They're happy to copy a command into a terminal
and read what comes back, but they aren't a deep technical user. They don't
know what a daemon, a graph, ACP or a worktree is, and they shouldn't need
to. They came to get one thing done.

## A guide

- **One task per guide**, the way the reader would say it: "Run your first
  graph", not "oe graph run". The title is that task.
- **Start with why.** Before the first command, say in one to three plain
  sentences what this lets them do and when they'd want it.
- **One action per step.** A step's title tells the reader what to do
  ("Type `oe graphs list` and press Enter."), and the text after it says
  what they'll see. Then show it.
- **Show what success looks like.** Every command they run gets a screenshot,
  with the part that matters highlighted and a label saying what it means in
  plain words ("Your graph is saved", not "registration row").
- **Give screenshots stable filenames.** Set `annotation.filename` (or pass the
  filename to `show`) using BEM: `guide__terminal--state.png`, for example
  `follow-a-run__terminal--stages-completed.png`. Keep each name unique across
  guides and steps so regeneration updates the same image.
- **Say what to do if it doesn't work** when there's a common way for it to
  go wrong ("If you see *cannot reach backend*, OpenEngine isn't running;
  start it with …").
- **End with where to go next**, pointing at the guide a reader would want
  after this one.

## The words

- Short sentences, everyday words. Prefer "background program" to "daemon",
  "list" to "enumerate", "save" to "register" when the meaning is the same.
- Explain an OpenEngine word the first time it appears on a page, using the
  glossary below, so every page explains it the same way.
- Leave out what the reader doesn't need: flags they won't use, internals,
  JSON, exit codes. If they must look at something technical, say what it
  means.
- Never let the test show through: no "verify", "assert", "expect",
  "fixture", "stub", "mock", or helper names. Setup the reader doesn't do
  goes in `[nodoc]` steps.

## Glossary

Use these explanations, in these words or close to them.

| Word | Say |
| --- | --- |
| OpenEngine | A tool that has AI coding agents work on your code, step by step, in the way you lay out. |
| `oe` | The command you type in a terminal to use OpenEngine. |
| agent | An AI coding assistant, such as Claude Code, that reads and changes code for you. |
| graph | A recipe for a piece of work: its steps, which agent does each one, and in what order. |
| run | One time a graph is carried out, from its first step to its last. |
| loop | A graph set to run again and again on a schedule, with limits on how much it may spend. |
| backend | A computer where OpenEngine's background program is running: this one, or another one such as a Mac mini. |
| `local` | The backend on this computer. It's there from the start. |
