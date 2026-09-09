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

## Database schema changes

- Generate new table and schema migration revisions with Alembic (`alembic revision`), and implement schema changes in those revisions rather than runtime DDL.
- Use `migrations/sqlite` for the state store and `migrations/sqlite_graph` for the separate graph runtime database. To generate graph revisions, use `alembic_config(database_url, store="graph")` with `alembic.command.revision`. Apply it with `engine-migrate --store graph <database_url>`.
- LangGraph owns its checkpoint database schema; leave its migrations to the checkpointer.
