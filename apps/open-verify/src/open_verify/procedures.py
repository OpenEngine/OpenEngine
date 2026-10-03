"""A frozen, locally retrieved procedural graph, independent of orchestration."""

import json
from importlib.resources import files


class Procedures:
    def __init__(self):
        self.graph = json.loads(files("open_verify").joinpath("procedures.json").read_text())

    def guidance(self, node: str) -> list[dict]:
        edges = self.graph["edges"]
        neighbors = {edge["to"] for edge in edges if edge["from"] == node}
        return [edge for edge in edges if edge["from"] in {node, *neighbors}]

    @staticmethod
    def locate(stage: str, observation: dict | None) -> str:
        if stage == "discover":
            return "discover"
        if observation is None:
            return "plan"
        result = observation.get("result", {})
        if (
            not observation["ok"]
            or result.get("timed_out")
            or result.get("exit_code") not in {None, 0}
        ):
            return "investigate"
        tool = observation["tool"]
        if tool == "start_process":
            return "launch"
        if tool in {"process_output", "browser_open", "browser_snapshot"}:
            return "observe"
        return "exercise"
