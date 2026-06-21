"""Add runtime state cache for bounded human request reads (US-168).

Revision ID: 0042
Revises: 0041
Create Date: 2026-06-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("human_game_runtime") as batch_op:
        batch_op.add_column(sa.Column("state_cache", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("human_game_runtime") as batch_op:
        batch_op.drop_column("state_cache")
