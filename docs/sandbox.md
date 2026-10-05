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
merge into their destination.

```toml
[sandbox]
backend = "process"
```

`process` is the default. It uses host executables, inherits the host environment,
and has the host's filesystem and network access. It is **not a security
boundary**. It rejects image selection and egress restrictions, including an
empty allowlist. `None` means unrestricted egress. Labels are metadata only.
`smolvm` is a recognized configuration value reserved for a future adapter;
selecting it currently fails explicitly at composition, without a host fallback.

This port is available through `Capabilities.sandbox` in the application
composition roots. Existing node and agent execution paths retain their current
host behavior; routing those paths through this port is follow-up work.

`tests/test_sandbox_contract.py` is the reusable contract suite. Add another
backend factory to its fixture parameters to run the same tests against it.
