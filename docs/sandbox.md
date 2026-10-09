# Sandbox execution

`engine.ports.Sandbox` provisions a disposable execution environment. Its
`create(SandboxSpec(...))` method is an async context manager: entry creates the
workspace, and exit destroys it even on exceptions or cancellation. The returned
`SandboxInstance` exposes `exec`, `copy_in`, `copy_out`, and idempotent `destroy`.
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
merge into their destination. Recursive copies preserve source symlinks and reject
existing destination symlinks instead of following them. Completed commands stop
remaining descendants in their process group before releasing cleanup tracking.

```toml
[sandbox]
backend = "process"
```

`process` is the default. It uses host executables, inherits the host environment,
and has the host's filesystem and network access. It is **not a security
boundary**. It rejects image selection and egress restrictions, including an
empty allowlist. `None` means unrestricted egress. Labels are metadata only.
`smolvm` selects the microVM adapter. It requires SmolVM 1.25.2 or newer and
an explicit guest image; unavailable hosts fail rather than falling back to
host execution. `engine doctor` (also `engine daemon doctor`) reports platform,
KVM access and binary/version availability. Missing SmolVM is a warning with
the default process backend and an error when the VM backend is selected.

This port is available through `Capabilities.sandbox` in the application
composition roots. Existing node and agent execution paths retain their current
host behavior; routing those paths through this port is follow-up work.

`tests/test_sandbox_contract.py` is the reusable contract suite. Add another
backend factory to its fixture parameters to run the same tests against it.

## SmolVM guest image

Build from the repository root using Docker or a compatible OCI builder. The
recipe includes Python 3.12 (for transfers), Node 22, Git, and the exact ACP
adapter versions used by `langgraph-acp`. The npm lock pins transitive packages
and platform binaries too; `npm ci` runs only while building the image. No npm
command or package installation happens during sandbox creation or execution.
Host Python must be 3.11.8+ for safe archive extraction. The base image tags
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
Workspaces are copied, never host-mounted. `exec --interactive` provides binary
streams without a TTY; secrets use temporary host environment references rather
than plaintext CLI arguments or persistent VM configuration. Commands do not
inherit the host's model/forge environment or SSH agent.

Destruction force-deletes the VM and its storage, including after partial
startup, exceptions, or repeated cancellation. A command timeout destroys the
whole instance to ensure its guest processes cannot survive a killed CLI;
that instance cannot be reused. Deletion failures are surfaced rather than
reported as successful cleanup. Startup orphan sweeping is follow-up #679.

For now, `egress_allowlist=None` uses unrestricted networking; any explicit
allowlist is rejected until #677 adds enforcement. Model login provisioning and
the terminal broker bridge remain #676/#678. Selecting this backend does not
route existing agents into VMs yet (#674), or change the default (#681).

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
