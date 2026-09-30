"""Append-only action evidence and human-readable session reports."""

import json
import re
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
        if tool in {"http_request", "run_command", "start_process", "process_output", "run_browser_test"}:
            relative = Path("actions") / f"{entry['id']}.json"
            receipt = self.path / relative
            receipt.parent.mkdir(exist_ok=True)
            receipt.write_text(json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8")
            entry["artifact"] = relative.as_posix()
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
        impact = state.get("impact")
        if impact:
            lines.extend(["Impact: " + impact["reason"], ""])
        if plan:
            lines.extend([plan["project_summary"], "", "## Plan", ""])
            for case in plan["cases"]:
                lines.extend([f"- {case['id']}: {case['title']} — {case['expected']}"])
            if plan["questions"] or plan["assumptions"]:
                lines.extend(["", "## Questions and assumptions", ""])
                lines.extend(f"- {item}" for item in plan["questions"] + plan["assumptions"])
        if state.get("setup_questions"):
            lines.extend(["", "## Setup questions", ""])
            lines.extend(f"- {question}" for question in state["setup_questions"])
        for result in state.get("findings", []):
            evidence = ", ".join(self.evidence_link(item) for item in result["evidence"])
            case_receipt = self.case_receipt(plan, result)
            lines.extend(
                [
                    "",
                    f"## {result['case_id']}: {result['status']}",
                    "",
                    result["actual"],
                    "",
                    f"Test-case receipt: [{result['case_id']}]({case_receipt})",
                    "",
                    "Evidence: " + (evidence or "none"),
                    "",
                ]
            )
            lines.extend(f"{i}. {step}" for i, step in enumerate(result["reproduction"], 1))
        lines.extend(
            [
                "",
                "Raw observations: evidence.jsonl. Executed command and HTTP receipts are in actions/. "
                "Screenshots and complete process logs are in this folder.",
                "",
            ]
        )
        (self.path / "report.md").write_text("\n".join(lines), encoding="utf-8")

    def case_receipt(self, plan: dict | None, finding: dict) -> str:
        case_id = finding["case_id"]
        filename = re.sub(r"[^A-Za-z0-9._-]", "_", case_id)
        receipt = self.path / "cases" / f"{filename}.json"
        receipt.parent.mkdir(exist_ok=True)
        case = next((item for item in (plan or {}).get("cases", []) if item["id"] == case_id), None)
        evidence = [
            item for item in self.observations if item["id"] in set(finding["evidence"])
        ]
        receipt.write_text(
            json.dumps({"case": case, "finding": finding, "evidence": evidence}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return receipt.relative_to(self.path).as_posix()

    def evidence_link(self, evidence_id: str) -> str:
        entry = next((item for item in self.observations if item["id"] == evidence_id), None)
        if entry is None or "artifact" not in entry:
            return evidence_id
        receipt = entry["artifact"]
        links = [f"[{evidence_id}]({receipt})"]
        log = entry["result"].get("log")
        if log:
            links.append(f"[complete log]({log})")
        body_file = entry["result"].get("body_file")
        if body_file:
            links.append(f"[response body]({body_file})")
        return " ".join(links)
