"""Append-only action evidence and human-readable session reports."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


class Artifacts:
    def __init__(self, parent: Path):
        self.path = parent / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid4().hex[:8])
        self.path.mkdir(parents=True, exist_ok=False)
        self.observations: list[dict] = []

    def write(self, name: str, value):
        (self.path / name).write_text(
            json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def record(self, tool: str, arguments: dict, result: dict, ok: bool) -> dict:
        entry = {
            "id": f"E{len(self.observations) + 1:04d}",
            "timestamp": datetime.now(UTC).isoformat(),
            "tool": tool,
            "arguments": arguments,
            "ok": ok,
            "result": result,
        }
        self.observations.append(entry)
        with (self.path / "evidence.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def report(self, state: dict):
        self.write("report.json", state)
        lines = [
            "# Open Verify",
            "",
            f"Request: {state['request']}",
            "",
            f"Status: **{state['status']}**",
            "",
            state.get("note", ""),
            "",
        ]
        plan = state.get("plan")
        if plan:
            lines.extend([plan["project_summary"], "", "## Plan", ""])
            for case in plan["cases"]:
                lines.extend([f"- {case['id']}: {case['title']} — {case['expected']}"])
            if plan["questions"] or plan["assumptions"]:
                lines.extend(["", "## Questions and assumptions", ""])
                lines.extend(f"- {item}" for item in plan["questions"] + plan["assumptions"])
        for result in state.get("findings", []):
            lines.extend(
                [
                    "",
                    f"## {result['case_id']}: {result['status']}",
                    "",
                    result["actual"],
                    "",
                    "Evidence: " + (", ".join(result["evidence"]) or "none"),
                    "",
                ]
            )
            lines.extend(f"{i}. {step}" for i, step in enumerate(result["reproduction"], 1))
        lines.extend(
            [
                "",
                "Raw observations: evidence.jsonl. Screenshots and process logs are in this folder.",
                "",
            ]
        )
        (self.path / "report.md").write_text("\n".join(lines), encoding="utf-8")
