# Security policy

OpenEngine runs coding agents with access to repositories, credentials, and
tools, so we take reports about it seriously.

## Reporting a vulnerability

Please report vulnerabilities privately rather than in a public issue or pull
request, so that a fix can ship before details are public.

- Preferred: open a private report through
  [GitHub security advisories](https://github.com/OpenEngine/OpenEngine/security/advisories/new).
- Otherwise: email [hello@openengine.cc](mailto:hello@openengine.cc) with
  "security" in the subject.

Include the affected version (`engine --version`), the interface involved
(CLI, daemon API, web UI, webhook, MCP, Slack), steps to reproduce, and the
impact you believe it has. Reproductions against your own local daemon with
synthetic data are ideal; please do not test against other people's
deployments.

We aim to acknowledge reports within three business days and to keep you
informed as we work on a fix. We will credit you in the release notes unless
you prefer otherwise.

## Supported versions

Security fixes are released for the latest minor version on
[GitHub releases](https://github.com/OpenEngine/OpenEngine/releases) and npm.
Upgrade with `npm i -g @openengine.cc/engine@latest` or by rerunning the
installer.

## How we review our own surface

OpenEngine runs a daily
[security review loop](docs/examples/graphs/security-review.yaml) on its own
public API surface. Confirmed findings become scoped GitHub issues; the policy
above governs whether a finding is published or handled privately first. The
[blog post](https://openengine.cc/blog/continuous-security-review/) describes
the process.
