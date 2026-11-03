"""add runtime broker credentials

Revision ID: 9d5590f3f1a5
Revises: 1b2226fa7c9a
Create Date: 2026-11-03 06:19:54.984684
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '9d5590f3f1a5'
down_revision: Union[str, Sequence[str], None] = '1b2226fa7c9a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('runtime_credentials',
    sa.Column('runtime_id', sa.String(length=128), nullable=False),
    sa.Column('generation', sa.Integer(), nullable=False),
    sa.Column('username', sa.String(length=128), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('issued_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('secret_digest', sa.String(length=64), nullable=True),
    sa.CheckConstraint("status IN ('active', 'revoking', 'retired')", name='ck_runtime_credentials_status'),
    sa.PrimaryKeyConstraint('runtime_id'),
    sa.UniqueConstraint('username', name='uq_runtime_credentials_username'),
    comment='Internal broker credential ownership and revocation state per runtime; never the secret.'
    )


def downgrade() -> None:
    # Retired rows are what keep revoked authority from being granted again.
    if op.get_bind().execute(sa.text("SELECT 1 FROM runtime_credentials LIMIT 1")).first():
        message = "Cannot downgrade after issuing runtime broker credentials"
        raise RuntimeError(message)
    op.drop_table('runtime_credentials')
