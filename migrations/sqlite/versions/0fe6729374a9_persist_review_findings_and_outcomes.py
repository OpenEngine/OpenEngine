"""persist review findings and outcomes

Revision ID: 0fe6729374a9
Revises: d487944fbd15
Create Date: 2026-10-05 16:22:06.901945
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0fe6729374a9'
down_revision: Union[str, Sequence[str], None] = 'd487944fbd15'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "review_findings",
        sa.Column("id", sa.Text(), primary_key=True),
        *[sa.Column(name, sa.Text(), nullable=False) for name in
          ("run_id", "node_id", "stage", "agent", "facet", "severity", "tagline", "description", "created_at")],
        *[sa.Column(name, sa.Text(), nullable=True) for name in
          ("model_tier", "file", "pr_url", "head_sha")],
        sa.Column("line", sa.Integer(), nullable=True),
        sa.Column("comment_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("stage IN ('raw', 'reranked', 'triaged', 'posted')", name="finding_stage"),
    )
    for name in ("run_id", "pr_url", "file"):
        op.create_index(f"ix_review_findings_{name}", "review_findings", [name])
    op.create_table(
        "finding_outcomes",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("finding_id", sa.Text(), sa.ForeignKey("review_findings.id"), nullable=False),
        sa.Column("signal", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.Text(), nullable=False),
        sa.UniqueConstraint("finding_id", "signal", name="uq_finding_signal"),
    )


def downgrade() -> None:
    op.drop_table("finding_outcomes")
    op.drop_table("review_findings")
