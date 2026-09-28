"""The service calls the workbench makes, and nothing else.

Built from the CLI's own request helpers so authentication and error wording
stay the same as every other `engine` command.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import quote

Fetch = Callable[[str, str], dict[str, Any]]
Post = Callable[[str, str, dict[str, Any]], dict[str, Any]]

#: Where the web service mounts the graph engine's own control surface.
GRAPH = "/graph/api"


class ServiceClient:
    def __init__(self, server: str, fetch: Fetch, post: Post) -> None:
        self.server = server
        self._fetch = fetch
        self._post = post

    def get(self, path: str) -> dict[str, Any]:
        return self._fetch(self.server, path)

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._post(self.server, path, body)

    def config(self) -> dict[str, Any]:
        return self.get("/api/config")

    def source_control(self) -> dict[str, Any]:
        return self.get("/api/source-control/status")

    def runs(self) -> list[dict[str, Any]]:
        runs = self.get("/api/runs").get("runs")
        return [run for run in runs if isinstance(run, dict)] if isinstance(runs, list) else []

    def run(self, run_id: str) -> dict[str, Any]:
        return self.get(f"/api/runs/{quote(run_id)}")

    def topology(self, graph_id: str) -> dict[str, Any]:
        return self.get(f"{GRAPH}/graphs/{quote(graph_id)}")

    def snapshot(self, run_id: str) -> dict[str, Any]:
        return self.get(f"{GRAPH}/runs/{quote(run_id)}?includeValues=true")

    def events(self, run_id: str, cursor: int) -> list[dict[str, Any]]:
        events = self.get(f"/api/runs/{quote(run_id)}/graph-events?cursor={cursor}").get("events")
        return [event for event in events if isinstance(event, dict)] if isinstance(events, list) else []

    def create_run(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.post("/api/runs", body)

    def steer(self, run_id: str, node_id: str, message: str) -> dict[str, Any]:
        return self.post(
            f"{GRAPH}/runs/{quote(run_id)}/steering", {"message": message, "node": node_id},
        )

    def decide(self, run_id: str, approval_id: str, decision: str) -> dict[str, Any]:
        return self.post(
            f"{GRAPH}/runs/{quote(run_id)}/approvals/{quote(approval_id)}",
            {"decision": decision},
        )


__all__ = ["ServiceClient"]
