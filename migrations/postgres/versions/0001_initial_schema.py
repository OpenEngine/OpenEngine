"""Reserve the initial PostgreSQL revision.

Revision ID: postgres_0001
Revises:
"""


revision = "postgres_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # TODO: Define the schema when OpenEngine has a need for PostgreSQL.
    pass


def downgrade() -> None:
    pass
