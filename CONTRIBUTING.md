# Contributing to OpenEngine

Thanks for your interest. OpenEngine is built in the open, by the same graphs
it ships, and contributions of every size are welcome: bug reports, docs
fixes, example graphs, and code.

## Ways to help

- **Report a bug or request a feature** with the
  [issue templates](https://github.com/OpenEngine/OpenEngine/issues/new/choose).
  A minimal reproduction and the output of `engine daemon doctor` make bugs
  much faster to fix.
- **Pick up a `good first issue`.** Issues labeled
  [`good first issue`](https://github.com/OpenEngine/OpenEngine/labels/good%20first%20issue)
  are scoped for a first contribution and have enough context to start without
  asking. Comment on one to claim it.
- **Share a graph.** If you have written a graph you use, add it to
  [`docs/examples/graphs`](docs/examples/graphs) with a short comment at the top
  explaining what it does and when to run it.
- **Improve the docs.** The docsite is in [`site/`](site); most pages have an
  "Edit this page" link. Pages under `site/docs/guides/test2doc-*` are generated
  from the end-to-end tests, so edit the tests instead.
- **Ask and answer questions** on
  [Slack](https://join.slack.com/t/openenginegroup/shared_invite/zt-49mkaebkz-m86SbPAwn_QNMPqsSgioYQ).

## Development setup

[docs/development.md](docs/development.md) covers running from source, the
development server, configuration, and migrations. The short version:

```bash
git clone https://github.com/OpenEngine/OpenEngine.git
cd OpenEngine
uv sync --all-packages
npm --prefix apps/web install
uv run engine-dev
```

The repository is a uv workspace. Runtime, graph, and CLI code live under
`packages/`, the web service under `apps/web`, the CLI under `cli/`, and the
v2 services under `v2/`. [ARCHITECTURE.md](ARCHITECTURE.md) explains how they
fit together.

## Making a change

1. Fork the repository and create a branch from `main`.
2. Make the change, with tests. `uv run pytest` runs the Python suites; the
   CI baselines for lint and types are described in
   [docs/ruff-baseline.md](docs/ruff-baseline.md) and
   [docs/pyright.md](docs/pyright.md).
3. Write commit messages as [Conventional Commits](https://www.conventionalcommits.org/)
   (`feat:`, `fix:`, `docs:`, `chore:`, with an optional scope such as
   `feat(cli):`). Release Please builds the changelog and version from them, so
   the first line of your commit is what users will read in the release notes.
   CI checks the format.
4. Open a pull request. Describe what changed and why, how you tested it, and
   link the issue it addresses. Keep PRs focused; several small PRs are easier
   to review than one large one.

You are welcome to run OpenEngine's own graphs on your change. `engine go
review <your-pr-url>` posts the same review findings a maintainer would see.

## Review and merging

A maintainer reviews every PR. Expect a first response within a few days; if
you have not heard back in a week, a polite ping on the PR or in Slack is
welcome. Once approved and green, a maintainer merges it, and the change ships
in the next release.

## Reporting security issues

Please do not open public issues for vulnerabilities. See
[SECURITY.md](SECURITY.md).

## Code of conduct

This project follows the [code of conduct](CODE_OF_CONDUCT.md). By
participating you agree to uphold it.

## License

By contributing, you agree that your contributions are licensed under the
[Apache License 2.0](LICENSE).
