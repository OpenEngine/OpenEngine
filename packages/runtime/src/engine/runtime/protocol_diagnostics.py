"""Privacy-conscious diagnostics shared by interactive agent transports."""

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

AGENT_PROTOCOL_DIAGNOSTIC_LOG = "ENGINE_AGENT_PROTOCOL_LOG"
AGENT_PROTOCOL_DIAGNOSTIC_MAX_BYTES = 1_000_000
AGENT_PROTOCOL_DIAGNOSTIC_BACKUPS = 3


def _rotate(path: Path) -> None:
    if not path.exists() or path.stat().st_size < AGENT_PROTOCOL_DIAGNOSTIC_MAX_BYTES:
        return
    oldest = path.with_name(f"{path.name}.{AGENT_PROTOCOL_DIAGNOSTIC_BACKUPS}")
    oldest.unlink(missing_ok=True)
    for index in range(AGENT_PROTOCOL_DIAGNOSTIC_BACKUPS - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        if source.exists():
            source.replace(path.with_name(f"{path.name}.{index + 1}"))
    path.replace(path.with_name(f"{path.name}.1"))


@dataclass(frozen=True)
class AgentProtocolDiagnostics:
    """One runner session's safe context and opt-in JSONL event sink.

    Callers must pass structural metadata only. Prompts, commands, approval
    wording, answers, schema property names, and property values do not belong
    in ``details``; the runner adapter is the boundary that understands which
    provider fields contain those values.
    """

    runner: str
    agent_run_id: str
    binary_path: str
    working_directory_sha256: str

    @classmethod
    def for_run(
        cls,
        runner: str,
        agent_run_id: object,
        binary_path: str,
        working_directory: str,
    ) -> "AgentProtocolDiagnostics":
        return cls(
            runner=runner,
            agent_run_id=str(agent_run_id),
            binary_path=binary_path,
            working_directory_sha256=hashlib.sha256(
                working_directory.encode()
            ).hexdigest(),
        )

    def record(self, event: str, **details: Any) -> None:
        configured = os.environ.get(AGENT_PROTOCOL_DIAGNOSTIC_LOG)
        if not configured:
            return
        path = Path(configured).expanduser()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _rotate(path)
            record = {
                "timestamp": datetime.now(UTC).isoformat(),
                "event": event,
                "runner": self.runner,
                "agent_run_id": self.agent_run_id,
                "binary_path": self.binary_path,
                "working_directory_sha256": self.working_directory_sha256,
                **details,
            }
            descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                os.write(
                    descriptor,
                    (json.dumps(record, sort_keys=True) + "\n").encode(),
                )
            finally:
                os.close(descriptor)
            path.chmod(0o600)
        except OSError as error:
            # Observability must never be able to terminate an agent turn.
            LOGGER.warning("could not write agent protocol diagnostic: %s", error)


__all__ = ["AGENT_PROTOCOL_DIAGNOSTIC_LOG", "AgentProtocolDiagnostics"]
