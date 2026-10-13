"""merge project bindings and workorder metadata

Revision ID: c3836c457cf8
Revises: c589719ca92b, 3a749697aced
Create Date: 2026-10-12 18:50:10.745731
"""

from typing import Sequence, Union



revision: str = 'c3836c457cf8'
down_revision: Union[str, Sequence[str], None] = ('c589719ca92b', '3a749697aced')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
