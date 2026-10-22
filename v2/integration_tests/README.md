Tests which cross service boundaries on running services. 

Tests here must cross the NATS boundary and are structured as NATS commands in -> results validated out. 

Tests here MAY NOT monkeypatch or otherwise reach into the code.

## Automatically provisioned VM flow

Requires smolvm, uv, nats-server and access to the Python image and PyPI. No
authenticated model image is needed:

```sh
uv run --project v2 python -m virtual_control_plane.provisioning.bundle /tmp/oe-runtime-bundle --fixture
NATS_SERVER=/absolute/path/to/nats-server \
OE_VCP_TEST_IMAGE=python:3.12-slim \
OE_VCP_RUNTIME_BUNDLE=/tmp/oe-runtime-bundle \
uv run --project v2 pytest v2/integration_tests/test_vcp.py \
  -k automatically_provisioned -o addopts=--import-mode=importlib
```

This uses public NATS commands against VCP and a real smolvm guest. A test-only
ACP subprocess exercises the production executor, privilege drop and cgroup
fencing. The flow creates, executes, continues, restarts VCP, cancels, restarts
the daemon, continues again, stops/starts the VM, continues after boot and destroys. Guest process/journal inspection checks
termination and exactly one launch per attempt. It is infrastructure validation,
not evidence of compatibility with an authenticated model adapter.
