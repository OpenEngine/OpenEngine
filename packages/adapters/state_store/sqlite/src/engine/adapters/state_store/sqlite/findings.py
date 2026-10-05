"""Findings in the state store's migrated SQLite database."""
from collections.abc import Sequence
from dataclasses import asdict
from hashlib import sha256
import sqlite3
from threading import RLock

from engine.domain.findings_ledger import ReviewFinding, timestamp


class SQLiteFindingsLedger:
    def __init__(self, connection: sqlite3.Connection, lock: RLock) -> None:
        self._connection = connection
        self._lock = lock

    def record_findings(self, findings: Sequence[ReviewFinding]) -> None:
        with self._lock, self._connection:
            for finding in findings:
                values = {"id": finding.id, **asdict(finding)}
                columns = ", ".join(values)
                placeholders = ", ".join("?" for _ in values)
                self._connection.execute(
                    f"INSERT INTO review_findings ({columns}) VALUES ({placeholders}) ON CONFLICT(id) DO NOTHING",
                    tuple(values.values()),
                )

    def attach_comment(self, finding_id: str, comment_id: int, *, pr_url: str, head_sha: str | None) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE review_findings SET comment_id = ?, pr_url = ?, head_sha = ? WHERE id = ?",
                (comment_id, pr_url, head_sha, finding_id),
            )

    def record_outcome(self, finding_id: str, signal: str, *, source: str, value: str | None = None) -> None:
        identity = sha256(f"{finding_id}:{signal}".encode()).hexdigest()
        with self._lock, self._connection:
            self._connection.execute(
                """INSERT INTO finding_outcomes (id, finding_id, signal, value, source, observed_at)
                   VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(finding_id, signal) DO NOTHING""",
                (identity, finding_id, signal, value, source, timestamp()),
            )

    def list_findings(self, *, pr_url: str | None = None, run_id: str | None = None) -> Sequence[ReviewFinding]:
        filters = {key: value for key, value in (("pr_url", pr_url), ("run_id", run_id)) if value is not None}
        where = " WHERE " + " AND ".join(f"{key} = ?" for key in filters) if filters else ""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM review_findings" + where + " ORDER BY created_at, id", tuple(filters.values()),
            ).fetchall()
        return [ReviewFinding(**{key: row[key] for key in row.keys() if key != "id"}) for row in rows]
