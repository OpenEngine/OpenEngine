"""Composition root for the web control interface.

The one file in this app allowed to name concrete adapters, and a sibling of the
other two compositions rather than shared code with them -- for the same reason
the worker's is: these processes will diverge, and sharing now would couple
three deployables that should be free to move independently.

Two things are built here, and only one of them is connected:

* `build_capabilities` wires every port to its implementation. The interface
  shows the result on its Wiring page, which is introspection of the graph --
  reading the type of each field -- not a call into it.
* `build_read_model` is where the pages' data will come from. It returns the
  empty read model, because this interface is deliberately unwired.

Wiring it up is a change to `build_read_model` alone: a `StateStore`-backed
`ReadModel` built from `build_capabilities(settings).state_store`. No page above
it changes, which is the point of the seam.
"""

from dataclasses import dataclass

from engine.adapters.codex import CodexAgentRunner
from engine.adapters.communications import BuzzCommunications
from engine.adapters.github import GitHubSourceControl
from engine.adapters.postgres import PostgresStateStore
from engine.adapters.temporal import TemporalWorkflowRuntime
from engine.adapters.workspace import GitWorktreeWorkspaceProvider
from engine.apps.web.readmodel import EmptyReadModel, ReadModel
from engine.runtime import Capabilities


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the interface needs from the environment.

    `host` and `port` are handed to Streamlit's own server by `__main__`; the
    rest are the adapter arguments, carried so this composition root can build
    the same graph its siblings do. Loading them from the environment lands with
    the deployment ticket, along with the other two.
    """

    host: str = "localhost"
    port: int = 8501
    temporal_host: str = "localhost:7233"
    github_token: str = ""
    buzz_base_url: str = ""
    buzz_api_token: str = ""
    workspace_root: str = "/tmp/engine-workspaces"
    postgres_dsn: str = ""


def build_capabilities(settings: Settings) -> Capabilities:
    """Wire every port to its concrete implementation.

    Construction only: every adapter here is a placeholder whose methods raise
    `NotImplementedError`, and none of them opens anything in `__init__`.
    """
    return Capabilities(
        workflow_runtime=TemporalWorkflowRuntime(settings.temporal_host),
        source_control=GitHubSourceControl(settings.github_token),
        agent_runner=CodexAgentRunner(),
        communications=BuzzCommunications(settings.buzz_base_url, settings.buzz_api_token),
        workspace_provider=GitWorktreeWorkspaceProvider(settings.workspace_root),
        state_store=PostgresStateStore(settings.postgres_dsn),
    )


def build_read_model(settings: Settings) -> ReadModel:
    """Where the pages get their data. Unwired: there is no data yet.

    The one line that changes when the state store lands.
    """
    return EmptyReadModel()


__all__ = ["Settings", "build_capabilities", "build_read_model"]
