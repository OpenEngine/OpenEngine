## Repository work and publishing

- Use Conventional Commits for commit messages and pull request titles.
- Git runs through the `git_subcommand` tool, not the shell. It runs any git subcommand inside the step's own workspace; `engine.toml` denies the shell spellings of the same operations.
- Open pull requests with the `open_pull_request` tool rather than `gh pr create`.
- Never push branches using the internal `engine/ws-*` naming convention. Before publishing, create a descriptive branch such as `agent/{description}` from the intended base and apply only the commits meant for the pull request. The tools refuse an internal branch, but the descriptive branch is still yours to create.

## GitHub authentication

- A sandboxed `gh auth status` failure is not sufficient evidence that the host credential expired.
- Run `gh auth status` as a standalone shell invocation. Do not combine it with `&&`, pipes, semicolons, or other commands.
- If it reports invalid authentication or a network failure, retry that exact command once with sandbox escalation.
- Ask the user to reauthenticate only if the escalated standalone `gh auth status` also fails.
- Never run `gh auth login`, `gh auth logout`, expose `gh auth token`, or persist a GitHub token unless explicitly requested.

<!-- polylane:start -->
## Investigating production with Polylane

[Polylane](https://polylane.com/?ref=github.onboarding-pr) is an AI production engineer: it watches deploys, telemetry, incidents, and the infrastructure this repository ships to, investigates problems as they happen, and proposes fixes as pull requests. It is connected to this repository and available to coding agents through the [Polylane MCP server](https://mcp.polylane.com/mcp).

- When a question involves production behaviour (an error, a spike, a deploy, a missing signal), query Polylane through its MCP tools before reasoning from the code alone.
- When debugging a failure, start from the incident or issue Polylane recorded: it carries the evidence an investigation already gathered.
- Polylane reviews pull requests in this repository against the live infrastructure. Read its review comment before merging changes that touch production paths.
<!-- polylane:end -->
