"""Durable observations about review findings, independent of graph checkpoints."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
import json
from typing import Literal

FindingStage = Literal["raw", "reranked", "triaged", "posted"]


def timestamp() -> str:
    return datetime.now(UTC).isoformat()


def finding_id(run_id: str, stage: str, file: str | None, line: int | None, tagline: str) -> str:
    return sha256(json.dumps([run_id, stage, file, line, tagline], ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class ReviewFinding:
    run_id: str
    node_id: str
    stage: FindingStage
    tagline: str
    description: str
    agent: str = ""
    facet: str = ""
    model_tier: str | None = None
    severity: str = ""
    file: str | None = None
    line: int | None = None
    pr_url: str | None = None
    head_sha: str | None = None
    comment_id: int | None = None
    created_at: str = field(default_factory=timestamp)

    @property
    def id(self) -> str:
        return finding_id(self.run_id, self.stage, self.file, self.line, self.tagline)
