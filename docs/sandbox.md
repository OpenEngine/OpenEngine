# Sandbox execution

Graph ACP nodes use **SmolVM by default**, including naming, specification,
implementation, all review facets, refutation, reranking and impact analysis in
the default workflow. Each agent connection gets its own VM. Workspace setup,
CI polling, approval handling, the orchestrator, host Git/forge operations and
MCP broker remain on the host. Slack/GitHub concierge agents and the scoper also
keep their existing host ACP launchers; `[sandbox]` changes graph execution.

Install SmolVM and build an architecture-matching guest image as described in
[Guest image](#smolvm-guest-image), then select it:

```sh
engine vm use smolvm --image /absolute/path/openengine-sandbox.tar
engine doctor
engine daemon restart
```

Omitting `[sandbox]` selects SmolVM too. Supported hosts are Apple Silicon macOS
and Linux with usable KVM. Missing prerequisites and unsupported hosts fail
explicitly; OpenEngine never silently switches graph agents to the host. To opt
out on any host, use `engine vm use process` and restart the service, or set:

```toml
[sandbox]
backend = "process"
```

Existing configurations explicitly selecting `process` keep that selection.
Custom agent commands must exist inside the image; the bundled image includes
Codex and Claude ACP adapters. Host `npx` paths and host stdio MCP commands do
not become guest executables automatically.


`engine.ports.Sandbox` provisions a disposable execution environment. Its
`create(SandboxSpec(...))` method is an async context manager: entry creates the
workspace, and exit destroys it even on exceptions or cancellation. The returned
`SandboxInstance` exposes `exec`, `working_directory`, `copy_in`, `copy_out`, and idempotent `destroy`.
Do not use an instance after destruction.

```python
import asyncio

from engine.ports import SandboxSpec

async with capabilities.sandbox.create(SandboxSpec(workspace=checkout, timeout=60)) as sandbox:
    process = await sandbox.exec([
        "python", "-c", "from pathlib import Path; Path('result.txt').write_text('done')"
    ])
    process.stdin.close()
    stdout, stderr, status = await asyncio.gather(
        process.stdout.read(), process.stderr.read(), process.wait()
    )
    await sandbox.copy_out("result.txt", output_path)
```

Commands take argv directly, without a shell. Stdio streams carry bytes; callers
must consume stdout and stderr concurrently for large outputs. `wait()` returns
the exit status, or raises `TimeoutError` when the per-command timeout expires.
The timer starts at execution, not when `wait()` is called. Destruction stops
outstanding commands (and their process groups on POSIX).

The spec carries an optional image, a directory to copy, secret environment
variables, an optional egress allowlist, a timeout in seconds, and labels.
Secrets are excluded from the spec's repr and override host and per-command
environment values. Workspace copies are disposable; use `copy_out` to retain
results. Copy paths and command working directories are workspace-relative and
cannot resolve outside it. Files and directories can be copied; directory copies
merge into their destination in the process backend and on SmolVM copy-in.
SmolVM copy-out requires a fresh path or an empty staging directory and rejects
Git metadata (`.git`, case-insensitive) in exported content or destination paths.
Copy selected results from staging through an explicit host policy; never merge
a guest workspace or its Git metadata directly back into a host checkout. The
graph runtime's patch policy is described below.
Recursive copies preserve source symlinks and reject
existing destination symlinks instead of following them. Completed commands stop
remaining descendants in their process group before releasing cleanup tracking.

```toml
[sandbox]
backend = "process"
```

`process` is the explicit opt-out. It uses host executables, inherits only allowlisted environment variables,
and has the host's filesystem and network access. It is **not a security
boundary**. It rejects image selection and egress restrictions, including an
empty allowlist. `None` means unrestricted egress. Labels are metadata only.
`smolvm` selects the microVM adapter. It requires SmolVM 1.25.2 or newer and
an explicit guest image; unavailable hosts fail rather than falling back to
host execution. `engine doctor` (also `engine daemon doctor`) reports platform,
KVM access and binary/version availability. Missing SmolVM is a warning with
the process opt-out and an error with the default VM backend.

Select the local sandbox backend with the CLI:

```sh
engine vm list                    # List backends; * marks the configured one
engine vm use process             # Explicitly use host subprocesses
engine vm use smolvm --image /absolute/path/to/openengine-sandbox.tar
```

`engine vm` still shows the configured backend, and `engine vm process` /
`engine vm smolvm` remain selection shorthands. In `engine vm list`, `*` marks the
configured selection, which a running service loads after restart.

`engine vm` uses `ENGINE_CONFIG` or the installed user's configuration. Add
`--config /path/to/engine.toml` to select another file and `--json` for structured
output. Selection updates only the sandbox settings, preserving other settings,
comments and file permissions. Switching to SmolVM requires a readable, nonempty
local image archive and a supported host with SmolVM 1.25.2 or newer. The command
checks the managed service's PATH when configured; it does not boot a VM or verify
the image's guest tools or architecture. An omitted `--image` reuses the configured
image, with relative paths resolved beside the config file. A supplied relative
image path resolves against the current directory and is saved as an absolute path.
Switching back to `process` keeps the image setting for later reuse and requires
no SmolVM installation. Failed checks leave configuration unchanged.

Restart running services after changing the backend (`engine daemon restart` for
the managed local service). The graph runtime routes ACP nodes through the
selected SmolVM backend. Existing connections are not moved. The explicit process setting
uses local subprocesses.

This port is also available through `Capabilities.sandbox` in application
composition roots. The separate legacy `AgentRunner` execution path retains its
host behavior; the VM routing described here applies to graph runtime ACP nodes.

## ACP agent nodes

The graph runtime passes a `SandboxConnectContext` on each connection, including
the run, node and execution identifiers, resolved workspace and per-node
`SandboxSpec`. Providers are reusable configuration; they do not retain an
invocation's workspace or connection. The continuation path receives the same
context fields. Context and credentials are not serialized into continuations.

`SandboxACPProvider`, exported by `engine.graph_runtime_langgraph`, launches ACP
over `SandboxInstance.exec` binary streams. The runtime adapts the built-in
Codex and Claude default commands to the image-installed `codex-acp` and
`claude-agent-acp`. Custom commands must be valid guest commands. The bundled
image does not include OpenCode; its default launcher is refused instead of
installing packages at runtime or falling back to the host.

For an explicitly configured node:

```python
from pathlib import Path
from engine.adapters.sandbox.smolvm import SmolvmSandbox
from engine.graph_runtime_langgraph import ACPNode, SandboxACPProvider, agent_registry
from engine.ports import SandboxSpec
from engine.runtime.model_credentials import host_model_credential

agents = agent_registry([
    SandboxACPProvider(
        name="codex",
        command=("codex-acp",),
        backend=SmolvmSandbox(image="/absolute/path/openengine-sandbox.tar"),
        model_provider="codex", credential_source=host_model_credential,
    ),
])
node = ACPNode(
    agent="codex", registry=agents, cwd=str(Path("checkout").resolve()),
    prompt="Read the repository and summarize its structure.",
    sandbox_spec=SandboxSpec(timeout=180),
)
```

`sandbox_spec` also accepts a callable of graph state, resolved once per
invocation. Do not put credentials into graph state; obtain them from a trusted
deployment credential source. The web runtime supplies `host_model_credential`
when adapting built-in Codex and Claude providers. Standalone runtimes opt in with
`LangGraphRuntime(..., model_credentials=host_model_credential)`; a directly
registered sandbox provider sets `model_provider` and `credential_source` as above.
Renamed built-in providers retain their underlying credential identity.

### Model credentials

Each model connection receives a new private home under `/dev/shm/oe-home-*`.
The guest must expose `/dev/shm` as tmpfs; otherwise initialization fails before
writing credentials. The home is outside the repository, with mode `0700`,
injected files `0600`, and a private umask. Host homes are never mounted. Files
arrive over the helper's stdin, never a host staging file, SmolVM argv, labels or
saved machine configuration. XDG directories and temporary files use this home.

For Codex, only allowlisted authentication fields from the selected
`CODEX_HOME/auth.json` (default `~/.codex/auth.json`) are copied into the guest's
`.codex/auth.json`. Its configuration is generated locally to select file auth;
host config, hooks, MCP settings and sessions are not copied. Keyring-only Codex
logins require a file cache or an explicitly supplied selected API key. This is
the documented [Codex auth-cache transfer](https://learn.chatgpt.com/docs/auth)
pattern, not a new browser login inside each VM.

For Claude, the resolver selects `CLAUDE_CODE_OAUTH_TOKEN`, an explicitly selected
API key, or an unexpired token from the chosen `CLAUDE_CONFIG_DIR` credentials
file. With the default account directory, it also considers the named macOS
Claude keychain entry and chooses the freshest token. Custom directories never
fall back to another account's keychain. Only the selected token is copied, not
the entire Claude store. Immediately before exec, the guest reads its private
token file into `CLAUDE_CODE_OAUTH_TOKEN` (or `ANTHROPIC_API_KEY` for API auth),
as supported by [Claude authentication](https://code.claude.com/docs/en/authentication).
Explicit per-node model credentials take priority over ambient login choices.

GitHub/GitLab token variables, SSH agent forwarding and Git credential-helper
configuration are not copied. ACP providers refuse explicit forge/helper env
overrides as well. Guest Git ignores system/global credential configuration;
workspace snapshots retain Git objects/refs but never the host's Git config.
The other model's credentials are not resolved or injected.

Refreshable Codex logins have one VM owner at a time, enforced by a file lock
beside the selected `auth.json` across Engine processes and runs. Parallel Codex
nodes sharing that login wait until its owner finishes; API-key nodes remain
parallel. The resolver rereads the login after acquiring ownership. Workspace
and session exports reread the guest credential files and add refreshed tokens
to the secret filter before accepting output. Live workspace checkpoints pause
the agent process group while capturing credentials and files.

Validated Codex rotation updates only the selected host login's token fields
and refresh timestamp, using an atomic replacement with mode `0600`. Account
and authentication mode changes are refused; arbitrary guest configuration is
never imported. Host-only metadata stays on the host. External changes to the
host login are detected before replacement and fail the node rather than being
overwritten. Standalone host Codex processes do not participate in this Engine
lock; avoid using the same refreshable login concurrently outside Engine.
Custom credential sources must supply a durable `source_path` for refreshable
Codex logins. Claude receives only its selected access token, without a refresh
token or a credential write-back path.

Teardown captures the final credential state before releasing ownership, even
when workspace or session export fails. If the host crashes or final capture
fails, a private marker containing only refresh-token fingerprints prevents reuse
of a possibly consumed refresh token. A fresh host sign-in is required before
that account can be used again; session archives contain no login files.
Expired/unavailable logins fail with a sign-in hint rather than falling back to
another account. The process sandbox refuses private-home specs because it
cannot provide this tmpfs isolation; ordinary host execution is unchanged.

The workspace handoff rejects source files containing the selected credential
and rejects credential-bearing output (including binary files) before generating
or applying a patch. Such a failure does not attach a credential-bearing patch.
Teardown removes the private home and destroys the VM on success, error or
cancellation. Forced VM deletion remains the final cleanup boundary if a guest
has replaced its helper. Secret values are excluded from configuration reprs and
redacted from transfer errors.

Run the real credential-isolation tests using synthetic logins, with no API keys
or model requests:

```sh
OE_SMOLVM_TEST_IMAGE="$HOME/.local/share/openengine/openengine-sandbox.tar" \
  uv run --all-packages pytest tests/test_model_credentials.py tests/test_credential_rotation.py -m integration -v
```

The connection owns a disposable sandbox context. ACP `session/new` receives
the sandbox's absolute working directory, never the host checkout path. Client
close, failed initialization, cancellation and timeouts release the sandbox.
Direct provider connections remain disposable. Graph runtime ACP nodes use the
workspace handoff below to retain accepted file changes between nodes.

### Per-node workspace handoff

Disconnected runs started without an explicit ref first copy the local checkout's
current files into a separate run worktree. This includes staged and unstaged
edits, deletions, and non-ignored untracked source folders; no preliminary commit
is required. The original checkout and its staging area are left unchanged.
Ignored files (such as build outputs and ignored `.env` files) are excluded.
An explicit ref or connected run continues to start from the selected committed
state. Host paths mentioned in prompts are not mounted: a local folder such as
`rust/snip` inside the repository becomes `/workspace/rust/snip` in the VM.

Workspace preparation reports the initial included file count and size, tracked,
changed, deleted and untracked counts, ignored entries, a bounded list of project
manifests and large files. Git change counts may overlap (for example, a deleted
file is also changed). Ignored directories are counted as entries, not recursively
inventoried. The report is visible as a `workspace_preflight` tool event and is
passed to agent nodes as initial workspace context. It reads metadata, not source
contents. Swift sources trigger a reminder that the SmolVM guest is Linux and
cannot execute a native macOS UI.

Empty inputs are noted so a planner can distinguish a new project from missing
existing code. Unsafe symlinks, unsupported file types and source trees exceeding
256 MiB or 10,000 entries fail workspace preparation with an explanation. Local
inputs are checked before copying into the run worktree. Ignore build outputs
or large data files to reduce the input. These are source checks: the sandbox
also enforces its full archive limits, including Git metadata and archive overhead,
at the transfer boundary. The inventory describes the initial input; agents still
need to inspect relevant files and verify available build tools.

Every sandbox-backed graph ACP invocation gets a fresh snapshot of the run's
current checkout, including tracked edits and non-ignored untracked outputs from
earlier nodes. Git worktrees are materialized as independent repositories with
their own objects and index, never a `.git` file pointing into the host. The
copy has branch history but no host remote configuration or hooks. Host process
execution without a sandbox retains its existing workspace behavior.
Git submodules are refused rather than silently omitted from a snapshot.

`ACPNode(workspace_policy="apply")` is the default for sandbox nodes. After the
agent finishes, its process group is stopped and files are exported into bounded
host staging. Guest Git metadata is excluded. A host-owned index compares these
files against the invocation's baseline and generates a binary-capable patch,
including additions, deletions and executable-bit changes. Guest commits do not
hide changes from this comparison. Ignored build outputs are not imported.

Only a successful node's patch is applied to the run checkout. This changes its
working tree, preserving the host branch history and staging area; guest commits
and branches are not imported or automatically published. Later nodes see the
accepted files in their own snapshots. Snapshotting and application are
serialized per checkout; agents execute concurrently. Concurrent patches that
touch different files can both be accepted. If a changed file no longer matches
the baseline, the branch changed, or Git refuses the patch, nothing from that
patch is applied and the node fails. Its `run.failed` event carries a base64
`workspace.patch` attachment, available as **Download patch** in the node view.

`ACPNode(workspace_policy="read-only")` removes write permissions from the
private copy and never imports its changes, even if the agent restores its own
guest permissions. `ReviewNode`, `RerankerNode`, naming, specification and impact
analysis use this policy in the bundled workflows. Parallel review facets share
neither a writable checkout nor authority to alter the run branch. This policy
does not make a process sandbox a security boundary. Failed or cancelled nodes
discard unaccepted edits. Explicit host-tool checkpoints described below remain
accepted even if a later tool or the node fails.

The terminal MCP bridge preserves this policy: read-only nodes can inspect a sanitized host snapshot with a restricted Git
command set, but cannot mutate host Git or publish. Writable nodes checkpoint their edits at explicit host
repository tool boundaries, as described below. YAML parallel facets also
receive the read-only workspace policy.

Exercise copy-in, copy-out, conflicts and parallel reviews without model credentials:

```sh
OE_SMOLVM_TEST_IMAGE="$HOME/.local/share/openengine/openengine-sandbox.tar" \
  uv run --all-packages pytest tests/test_node_workspace_copy.py -k smolvm-real -v
```

These real VM cases are skipped unless an image is supplied. The default suite
runs the same scenarios through process sandboxes and the SmolVM CLI fake.

Sandboxed Codex and Claude nodes restore their session store before ACP
`session/load`. Human approval recovery and always-open steering on the same
provider reload the original session in a fresh VM. Switching providers still
starts a new conversation with the runtime's transcript replay. Custom ACP
commands without a selected model provider retain the explicit session-load
refusal; their persistence layout must be implemented separately.

Only `$CODEX_HOME/sessions` or `$CLAUDE_CONFIG_DIR/projects` is exported after
the agent stops and before VM destruction, including failure and cancellation.
Credentials, model configuration, MCP tokens and the rest of HOME are excluded.
The selected credential is provisioned afresh on each connection. Session
archives are bounded, reject symlinks and injected credential values, and replace
the previous checkpoint atomically only after host validation. Treat them as
private conversation data.

SQLite deployments store checkpoints alongside the runtime database in
`<database>.agent-sessions/<run hash>/<session-key hash>/<provider>/state.tar`.
Directories are private (0700), archives are 0600, and unrelated runs, nodes and
providers have separate stores. In-memory runtimes use temporary storage owned
by their metadata store. Deleting a run after stopping it deletes both its ACP
continuation records and exported files. Shutting down the daemon retains
SQLite-backed checkpoints for recovery.

An abrupt host/VM crash cannot export the active turn: recovery can use only
files saved at the previous completed teardown. Startup orphan sweeping removes
abandoned VMs; it does not extract their in-flight conversations.

Run the VM transfer/resume checks without model credentials:

```sh
OE_SMOLVM_TEST_IMAGE="$HOME/.local/share/openengine/openengine-sandbox.tar" \
  uv run --all-packages pytest tests/test_sandbox_sessions.py -v
```

These tests use deterministic ACP peers in real VMs. Authenticated end-to-end
checks against the actual Codex/Claude adapters require model access.
Invocation-bound `TerminalMcpServer` bindings are bridged into the guest. Other
host stdio MCP commands remain refused before launch; they require their own
explicit guest implementation. Remote HTTP MCP endpoints must be granted in
the node's network policy.

### Agent environments

Local ACP launches and the process sandbox inherit only named variables in
their published environment allowlists: executable lookup, home/user, locale,
temporary directories, XDG locations and required platform variables. API keys,
forge tokens, proxy settings, `SSH_AUTH_SOCK` and arbitrary agent-specific
variables are not inherited. Explicit provider configuration extends this set;
`SandboxSpec.secrets` overrides conflicting values. Selected model credentials
use the private home above; its managed paths cannot be overridden by node env.
Other sandbox provider env values use secret references, never plaintext values
in SmolVM CLI arguments.

The local Claude provider additionally permits the named `CLAUDE_CONFIG_DIR`
path selected by the host application. This does not permit other agent settings
or copy the host account directory into a VM.

Guest execution filters image/runtime defaults to its own allowlist plus
explicit command/spec variables. Guest defaults supply guest paths; the host's
PATH or HOME is never copied into the VM implicitly. The host SmolVM control
process keeps its separate minimal environment, including SmolVM library paths.

`tests/test_sandbox_contract.py` is the reusable contract suite. Add another
backend factory to its fixture parameters to run the same tests against it.

## SmolVM guest image

Build from the repository root using Docker or a compatible OCI builder. The
recipe includes Python 3.12 (for transfers), Node 22, Git, and the exact ACP
adapter versions used by `langgraph-acp`. The npm lock pins transitive packages
and platform binaries too; `npm ci` runs only while building the image. No npm
command or package installation happens during sandbox creation or execution.
Archive extraction requires Python 3.11.13+, 3.12.11+, 3.13.4+, or 3.14+
on both host and guest, which include the
[June 2025 tarfile filter fixes](https://www.python.org/downloads/release/python-31113/).
Older patch releases are excluded by package metadata and rejected before
VM creation or archive extraction. The base image tags
track patch updates; export the built archive or pin the resulting OCI digest
for deployments that must use an identical image.

```sh
# Apple Silicon; use linux/amd64 for an x86_64 Linux KVM host.
docker build --platform linux/arm64 \
  -f packages/adapters/sandbox/smolvm/guest/Dockerfile \
  -t openengine-sandbox:1.3.0 .
docker save openengine-sandbox:1.3.0 -o /absolute/path/openengine-sandbox.tar
```

SmolVM accepts local Docker/Podman archives and OCI registry references; a
locally built Docker tag alone is not visible to SmolVM. Use an absolute
archive path in deployment configuration (or an immutable registry digest):

```toml
[sandbox]
backend = "smolvm"
image = "/absolute/path/openengine-sandbox.tar"
```

Install the full [SmolVM release](https://github.com/smol-machines/smolvm/releases/tag/v1.25.2)
(including bundled libraries/rootfs) and put its `smolvm` wrapper on the service
PATH. Supported OE hosts are macOS 11+ on Apple Silicon and Linux x86_64/arm64
with an accessible `/dev/kvm` using API version 12. Doctor is a prerequisite
probe, not a VM boot/image validation test.

Each sandbox creates an independently named `oe-sandbox-*` VM with the spec's
labels, starts a persistent workload, and copies a standalone transfer helper
outside `/workspace`. The image must provide the helper interpreter at
`/usr/local/bin/python3`, so command PATH overrides do not affect helper startup.
File copies use single-file archives over `machine cp`; archive traversal,
escaping links and existing destination symlinks are refused.
Links must stay within the transferred tree at both extraction and destination;
internal relative links are preserved, while standalone symlinks are rejected.
Copying a file into an existing directory preserves its original basename.
Transfers accept uncompressed tar archives with at most 256 MiB of archive data,
256 MiB of extracted contents and 10,000 members. Copy-out streams are bounded
by the host before writing excess bytes; extraction limits are checked before
each member is extracted and before copying to the destination. Hard links are
rejected to prevent amplification when files are copied. Rejected transfers
clean up host temporary files and attempt guest archive removal. If removal
also fails, the original transfer error is preserved with a cleanup failure note;
a cleanup failure after an otherwise successful transfer is raised normally.
Host SmolVM control/helper commands have a combined stdout/stderr limit of
1 MiB (32 MiB for the host-owned machine registry listing). Exceeding it terminates the CLI and raises an error without buffering
the remaining output. This also applies when guest code replaces the helper.
Agent command streams remain caller-consumed streams.
Workspaces are copied, never host-mounted. `exec --interactive` provides binary
streams without a TTY; secrets use temporary host environment references rather
than plaintext CLI arguments or persistent VM configuration. Commands do not
inherit the host's model/forge environment or SSH agent.

Destruction force-deletes the VM and its storage, including after partial
startup, exceptions, or repeated cancellation. A command timeout destroys the
whole instance to ensure its guest processes cannot survive a killed CLI;
that instance cannot be reused. Deletion failures are surfaced rather than
reported as successful cleanup. Startup recovery is described below.

## Egress policy (#677)

Every sandbox-backed ACP connection restricts outbound access to its selected
model provider plus the node's explicit grants. Codex permits `api.openai.com`,
`auth.openai.com`, and `chatgpt.com`. Claude permits `api.anthropic.com`,
`console.anthropic.com`, `claude.ai`, and `platform.claude.com`. Defaults follow
the provider type even when a registry entry is renamed. Custom ACP providers
without a model provider start with no outbound network access.

Python nodes use `allowed_hosts=("registry.npmjs.org",)`; YAML agent steps use
`allowed_hosts: [registry.npmjs.org]` (see [graph language](../cli/GRAPH_LANGUAGE.md)).
These are additional, exact DNS names, normalized to lowercase and deduplicated.
URLs, ports, wildcards, IP addresses, CIDRs and templates are rejected. An empty
list adds no hosts. Grants apply per invocation and to each parallel facet;
resuming a node reconstructs the same policy. Package downloads, remote MCP
hosts, telemetry, and redirects to other hosts need explicit grants.

SmolVM uses its host-side `virtio-net` stack with repeated
`--allow-host-pattern` flags. We avoid legacy `--allow-host`, which also permits
subdomains. Guest root cannot change this host-side policy. The underlying
[SmolVM network configuration](https://github.com/smol-machines/smolvm#networking)
provides DNS and connection enforcement; OE does not install guest firewall rules
or forward the host proxy.

At the lower-level sandbox port, `SandboxSpec.egress_allowlist=None` explicitly
requests unrestricted networking; a tuple restricts access to those exact hosts,
and `()` creates a VM without networking. ACP always supplies a restricted tuple:
a raw spec with `None` cannot turn off its provider policy. Explicit spec entries
are additional grants alongside `ACPNode.allowed_hosts` and provider defaults.
The process sandbox rejects all egress policies because it cannot enforce them.
Ordinary host ACP execution keeps the existing host network access; declaring
`allowed_hosts` on a host node fails rather than silently ignoring a restriction.

Provider API/auth defaults are kept narrow; enterprise gateways and future new
endpoints require explicit grants. Reference documentation:
[OpenAI networking](https://help.openai.com/en/articles/9247338-network-recommendations-for-chatgpt-errors-on-web-and-apps),
[Claude network configuration](https://code.claude.com/docs/en/network-config).
Graph nodes default to SmolVM; process execution requires an explicit opt-out.

## Terminal MCP bridge (#678)

A sandbox node receives a standalone Python stdio MCP shim in its private tmpfs
home. The shim forwards MCP JSON-RPC to the invocation's host `TerminalMcpBroker`
through a private Unix socket carried by SmolVM's `--mount-socket` transport.
No host Python paths, Engine installation or forge credentials are installed in
the guest. This control channel also works with outbound networking disabled;
it does not grant general host-loopback access or broaden the egress allowlist.

The broker creates a fresh 256-bit token bound to the run, node execution and
its granted tools. Every forwarded request is authenticated. The token is
in a mode-0600 guest manifest under its mode-0700 tmpfs home, outside the repo;
it is absent from agent/MCP argv, VM configuration and command logs. The host
endpoint is mode 0600 inside a private temporary directory and is removed when
the binding exits, including cancellation. Other invocations' tokens are refused.
The token joins the output credential scan, so leaking it into a workspace
refuses patch creation instead of attaching a credential-bearing diff.

Tool discovery, validation, completion, clarification, Git approvals and forge
operations all remain in the existing host broker. Disconnected mode still
withholds forge tools and rejects network Git. Only a broker-owned binding
provides the bridge capability: matching a server name or Python command does
not authorize forwarding an arbitrary host MCP server.

Before an approved host repository operation, a writable node exports its current
files, validates them and applies the usual conflict-checked patch to the run
checkout under its workspace lock. The SourceControl operation then runs on the
host against that checkout, using existing host credentials and approvals.
Afterwards, a fresh sanitized host snapshot refreshes the guest's files and Git
metadata. Guest hooks, config, index and commits are never imported. Ignored
build caches are retained where they do not obstruct refreshed tracked paths.
The refresh file list is streamed over stdin, so repositories with many or long
file names do not exceed the guest's command-line argument limit.
API/Git operation errors remain correctable tool errors; a workspace conflict,
credential leak or refresh failure fails the node. Conflicts retain the patch.

This checkpoint is explicit copy-back while the node is running. Once an approved
host operation has committed or pushed work, later node failure/cancellation
cannot undo that checkpoint or external operation. Edits made after the last
checkpoint still follow normal success-only copy-out. Read-only nodes reject
host repository mutation even when the guest changes its own permissions or the
user accepts a Git approval. Safe Git inspection (`status`, `diff`, `log`, `show`,
`rev-parse`, `ls-files`, `ls-tree`, branch listing) runs against the host-owned
private snapshot with a restricted option set and external diff/textconv disabled. A denied Git approval never enters the checkpoint.

Acceptance checks use a real local Git remote and a recording host HTTP forge
endpoint, without creating public PRs or consulting real forge credentials:

```sh
OE_SMOLVM_TEST_IMAGE=/absolute/path/openengine-sandbox.tar \
  uv run --all-packages pytest tests/test_sandbox_terminal_bridge.py -v
```

These exercise an ACP node launching the guest shim, host Git add/commit/push,
host PR creation, manual approval decisions, read-only review copies, conflicts,
credential-bearing output, token isolation and teardown. Actual Codex/Claude
model conversations remain covered by the separate opt-in compatibility tests.

## VM lifecycle (#679)

`ACPNode(timeout=1800)` defaults to a 30-minute wall-clock deadline. YAML agent
steps accept the same `timeout` field in seconds. Each invocation/facet has its
own deadline covering startup, copying in the workspace, model work, MCP tool
calls and approval waits. Values must be finite and positive; zero and null do
not disable deadlines. The low-level `SandboxSpec.timeout` remains a separate
per-command budget. Human/CI waits retain their existing behavior.

Sandbox adapters and workspace wrappers keep ownership of spawned tasks and
filesystem workers until they settle, even through repeated cancellation. A
worker failure takes precedence over deferred cancellation. On success, a
resource is registered before cancellation is propagated. During a failed ACP
connection, cleanup completes first; the original connection error remains
primary, with cleanup failures attached as notes.

Success, failure, deadline expiry, run cancellation and runtime shutdown close
the ACP client and its sandbox context. Teardown kills agent process groups,
closes the terminal broker and force-deletes the VM and its private home. Runtime
stop paths share and shield cleanup tasks, so repeated cancellation cannot make
shutdown return while another stop still owns a running VM. Cleanup may exceed
the invocation deadline and is always awaited. Final unaccepted workspace edits
are applied only after a successful invocation and teardown; earlier approved
host-tool checkpoints remain accepted, as described in #678.

Every VM carries `run`, `node`, and `execution` labels from its ACP context plus
protected `openengine.sandbox`, `openengine.scope`, and `openengine.lease`
labels. The scope is derived from the canonical graph-state directory. A private
mode-0600 lease file under `graph-state/sandbox-leases/` is locked by the host
for the VM's lifetime. Process death releases the lock even when Python finally
blocks cannot run; there is no stale PID test that could mistake PID reuse for
a live owner. Native subprocesses do not inherit the lease descriptor.

When SmolVM is configured, before exposing the durable graph runtime or restoring
runs, daemon startup lists SmolVM machines and force-deletes matching managed VMs whose leases are
unlocked. Active leases, other graph-state directories and unmanaged VMs are
left alone. Identity labels are reserved and cannot be overridden by node
labels. Recovery errors prevent the graph runtime from being offered. A retry
after fixing the backend requires a fresh runtime/daemon startup. This reclaims
resources; it does not restore an ACP session or replay model work.

Direct adapter users can call `await backend.sweep_orphans()` at startup and
supply a stable `state_directory`. Daemon composition supplies its graph-state
path automatically. Standalone adapter instances use a per-user temporary
namespace. VMs created before ownership labels were added cannot be safely
attributed to this deployment and are left for manual cleanup.

```sh
OE_SMOLVM_TEST_IMAGE=/absolute/path/openengine-sandbox.tar \
  uv run --all-packages pytest tests/test_sandbox_lifecycle.py -v
```

This includes real success/failure/deadline/cancel/shutdown checks and a child
host process killed with SIGKILL while its ACP agent is active. The test confirms
its VM survives the host kill, then verifies daemon startup removes it before
the graph runtime is offered. Other cases verify live and foreign VMs are kept.
No model or forge credentials are needed.

## Real hardware contract checks

The default suite runs the process backend and a subprocess-level SmolVM CLI
fake. Real VM cases are skipped unless an image is explicitly supplied. Run
the same contract on both macOS Apple Silicon and Linux KVM after building
the matching architecture image:

```sh
OE_SMOLVM_TEST_IMAGE=/absolute/path/openengine-sandbox.tar \
  uv run --all-packages pytest tests/test_sandbox_contract.py -k smolvm
```

Set `OE_SMOLVM_EXECUTABLE` to the wrapper's absolute path when it is not on PATH.
Failed prerequisite checks or boot failures are test failures once opted in,
rather than skipped tests. Real tests need permission to use the hypervisor,
write SmolVM's state directories and access the registry if using a remote image.


Real ACP agent acceptance tests are separate from the hardware contract:

```sh
OE_SMOLVM_TEST_IMAGE=/absolute/path/openengine-sandbox.tar \
  uv run --all-packages pytest tests/test_smolvm_acp_compatibility.py -m compatibility -v
```

Sign in to the host's selected Codex/Claude account, or supply `OPENAI_API_KEY`
and `ANTHROPIC_API_KEY` explicitly in the test process's managed environment.
The tests inject only the selected provider's credential
into each guest and record environment variable names, never credential values.
Once an image is supplied, missing credentials, failed prerequisites, boot errors
and provider errors fail the tests. Without an image the tests skip; ordinary
PR tests exercise real stdio with a deterministic ACP peer through the SmolVM CLI fake.
Local host ACP and the process sandbox have separate transport and lifecycle tests.

Credential-free real network checks for both provider policies and YAML grants:

```sh
OE_SMOLVM_TEST_IMAGE=/absolute/path/openengine-sandbox.tar \
  uv run --all-packages pytest tests/test_sandbox_egress.py -v
```

These open TCP connections to provider API/auth endpoints and `example.com`,
then assert unrelated DNS names, an ungranted subdomain and a direct external IP
are blocked. They do not send model requests or load host model credentials.
Without an image these cases skip; once opted in, network failures fail the test.


## Troubleshooting the default backend

- **Unsupported host, missing KVM or missing `smolvm`:** run `engine doctor` in
  the same service environment. Enable virtualization and grant `/dev/kvm`
  access on Linux, or use Apple Silicon on macOS. Install the complete SmolVM
  release, including its libraries/rootfs. `engine vm use process` is the
  explicit opt-out for unsupported machines.
- **Guest image not configured or unreadable:** build the matching architecture
  image and select its nonempty local archive with `engine vm use smolvm --image`.
  Restart existing services after changing settings.
- **Model login missing:** log in to the selected provider on the host. Only its
  selected credential enters the guest's private tmpfs home; forge credentials,
  host config and arbitrary credential helpers are excluded. Keyring-only Codex
  accounts need the file-backed credential described above.
- **Network requests fail:** model/auth hosts are granted by provider defaults.
  Declare extra exact `allowed_hosts` on the Python/YAML node; package registries
  and arbitrary websites are blocked unless granted. The host MCP bridge does
  not require broad network access.
- **A host stdio MCP command is refused:** use the supported terminal broker
  bridge or provide an explicit guest-compatible integration. Pushing and
  opening PRs stay on the host with existing approvals.
- **Resume fails after a crash:** checkpoints survive normal VM teardown, but an
  abrupt host kill can lose the active turn before export. Inspect the run's
  failure and saved session files; no silent replacement session is created.

## Hardware CI

`.github/workflows/sandbox.yml` provisions and tests the sandbox from this
repository. Push updates to an open PR to run it automatically. Once this
workflow exists on the default branch, it can also be started through
**Actions → sandbox → Run workflow**:

1. A GitHub-hosted `ubuntu-24.04-arm` job builds the ARM64 guest from the checked-in
   Dockerfile and npm lock, then uploads the archive as a short-lived artifact.
   This job only builds an image; it does not run Linux KVM tests.
2. The `[self-hosted, macOS, ARM64]` runner from #662 reuses its local guest archive
   when the Dockerfile and npm manifests are unchanged. On a cache miss it downloads
   the artifact and saves it under
   `$RUNNER_TOOL_CACHE/openengine-sandbox-images/v1/linux-arm64/<input-hash>/`.
   If `RUNNER_TOOL_CACHE` is unset, it uses `$HOME/.cache` instead. Downloads are
   checked as tar archives and published atomically so interrupted copies are not reused.
   The runner also installs the full SmolVM 1.27.1 release through its immutable upstream installer.
   The installation is cached under `$RUNNER_TOOL_CACHE/openengine-smolvm/1.27.1`.
   Docker is not needed on the Mac, and shell profiles are not modified.
3. The Mac checks hardware support, then runs the real sandbox suite and the
   complete default workflow with synthetic peers and credentials.

The image cache lives on that Mac's disk; another runner needs its own copy.
The hosted build still runs on every workflow, but a local cache hit avoids the
large artifact download. Changing the Dockerfile or either npm manifest selects
a new cache entry. To refresh packages from mutable base image tags without an
input change, delete the matching cache directory on the runner before rerunning.
Old entries can be removed when no sandbox job is using them.

No repository variables are required. Operators can still set
`SMOLVM_EXECUTABLE` to an absolute wrapper path, or `SMOLVM_TEST_IMAGE` to an
absolute prebuilt archive path on the Mac. An image override skips the hosted
build; it must be rebuilt whenever the guest Dockerfile or npm lock changes.
Missing prerequisites, failed provisioning and empty images fail the job rather
than silently skipping real-VM tests. Fork PRs stay off the self-hosted machine,
as in #662. The Mac runner must already exist and allow hardware virtualization;
the workflow installs software on that runner, not a runner service or host VM.

The default-workflow test replaces only the model peers, keeps the real graph
and MCP bridge, and forbids host ACP provider connections. It runs all agent
stages, copies an implementation change back, fans out reviews and accepts the
human review. No model API or real forge credentials are used.

The suite separates shared policy from backend behavior to limit repeated VM
boots. The sandbox contract checks streams, transfers, environment filtering and
cleanup on every backend. Workspace patch coordination (visibility, conflicts,
concurrent acceptance and cancellation) runs once on the process backend;
round-trip exports, hostile Git metadata, read-only reviews and graph execution
still run through SmolVM. Session tests keep both provider layouts and distinct
permission-recovery and human-review/steering paths. Credential and symlink
rejection cases each restore a valid checkpoint before testing rejection, so a
separate successful round-trip matrix is unnecessary.

Run the default workflow check locally without model credentials:

```sh
OE_SMOLVM_TEST_IMAGE=/absolute/path/openengine-sandbox.tar \
  uv run --all-packages pytest tests/test_default_workflow_sandbox.py -m integration -v
```

**#681 remains incomplete:** Linux KVM CI is deferred by agreement. Its
requirement to run the sandbox suite on both platforms is still outstanding.
The same hardware suite can be run manually
on a Linux KVM host with an architecture-matching image and
`OE_SMOLVM_TEST_IMAGE` set.
