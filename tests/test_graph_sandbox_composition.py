"""Deployment configuration must reach the graph runtime, not just capabilities."""

import asyncio
from types import SimpleNamespace

import pytest

from engine.adapters.sandbox.smolvm import SmolvmSandbox
from engine.apps.web.composition import build_graph_runtime


@pytest.mark.parametrize("backend", ["process", "smolvm"])
def test_configured_backend_reaches_graph_runtime(tmp_path, backend):
    image = str(tmp_path / "guest.tar")
    settings = SimpleNamespace(
        graph_state_directory=str(tmp_path / "graph-state"),
        engine_config=SimpleNamespace(
            public_url="", sandbox=SimpleNamespace(backend=backend, image=image),
        ),
    )

    async def run():
        # Opening a runtime must not boot a VM. Provisioning belongs to an
        # actual agent connection, after its per-node workspace is resolved.
        async with build_graph_runtime(settings, []) as runtime:
            if backend == "smolvm":
                assert isinstance(runtime.sandbox, SmolvmSandbox)
                assert runtime.sandbox.image == image
            else:
                assert runtime.sandbox is None

    asyncio.run(run())
