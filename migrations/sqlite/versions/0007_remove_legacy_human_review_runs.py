"""Remove workflow runs with the retired human-review phase.

Revision ID: sqlite_0007
Revises: sqlite_0006
"""

from alembic import op


revision = "sqlite_0007"
down_revision = "sqlite_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM run_states
        WHERE CASE
            WHEN json_valid(state_json)
            THEN json_extract(state_json, '$.phase') = 'awaiting_human_review'
            ELSE 0
        END
        """
    )


def downgrade() -> None:
    # Deleted prerelease workflow runs cannot be reconstructed.
    pass
