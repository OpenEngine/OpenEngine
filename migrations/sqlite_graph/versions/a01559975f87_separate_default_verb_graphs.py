"""separate default verb graphs

Revision ID: a01559975f87
Revises: c3836c457cf8
Create Date: 2026-10-12 19:19:12.589602
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import json


revision: str = 'a01559975f87'
down_revision: Union[str, Sequence[str], None] = 'c3836c457cf8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    for name, raw in connection.execute(sa.text("SELECT name, verbs FROM cli_projects")):
        verbs = json.loads(raw)
        for verb in ("review", "fix"):
            if verbs.get(verb) == "implementation-review-rerank":
                verbs[verb] = verb
        connection.execute(sa.text("UPDATE cli_projects SET verbs = :verbs WHERE name = :name"),
                           {"name": name, "verbs": json.dumps(verbs)})


def downgrade() -> None:
    # Bindings are user configuration; downgrading must not overwrite later edits.
    pass
