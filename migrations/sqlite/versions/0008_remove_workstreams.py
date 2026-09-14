"""Remove workstreams, rehoming their runs on the milestone above them.

Revision ID: sqlite_0008
Revises: sqlite_0007
"""

from alembic import op


revision = "sqlite_0008"
down_revision = "sqlite_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A run started in a workstream is still a record of work done for that
    # workstream's milestone, so it is moved up a level rather than dropped
    # with the heading it hung from. The row's own column and the state it
    # serialises are both rewritten: the adapter reads the run back out of the
    # JSON, and the column is what the milestone query filters on.
    op.execute(
        """
        UPDATE run_states
        SET milestone_id = (
            SELECT milestone_id FROM workstreams
            WHERE workstreams.workstream_id = run_states.workstream_id
        )
        WHERE workstream_id IS NOT NULL AND milestone_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE run_states
        SET state_json = json_set(
            json_remove(state_json, '$.workstream_id'),
            '$.milestone_id',
            milestone_id
        )
        WHERE json_valid(state_json) AND milestone_id IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE run_states
        SET state_json = json_remove(state_json, '$.workstream_id')
        WHERE json_valid(state_json) AND milestone_id IS NULL
        """
    )
    with op.batch_alter_table("run_states") as batch:
        batch.drop_index("runs_by_workstream")
        batch.drop_column("workstream_id")
    op.drop_table("workstreams")


def downgrade() -> None:
    # The workstreams a run belonged to cannot be reconstructed from milestones.
    raise NotImplementedError("workstreams cannot be restored once removed")
