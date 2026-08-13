## GitHub authentication and publishing

- Never push branches using the internal `engine/ws-*` naming convention. Before publishing, create a descriptive branch such as `agent/{description}` from the intended base and apply only the commits meant for the pull request.
- A sandboxed `gh auth status` failure is not sufficient evidence that the host credential expired.
- Run `gh auth status`, `git push`, and `gh pr create` as separate shell invocations. Do not combine them with `&&`, pipes, semicolons, or other commands.
- If a standalone GitHub command reports invalid authentication or a network failure, retry that exact command once with sandbox escalation.
- Ask the user to reauthenticate only if the escalated standalone `gh auth status` also fails.
- Never run `gh auth login`, `gh auth logout`, expose `gh auth token`, or persist a GitHub token unless explicitly requested.
