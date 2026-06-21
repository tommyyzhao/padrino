"""Add server default for human runtime buffer snapshots.

Revision ID: 0043
Revises: 0042
Create Date: 2026-06-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("human_game_runtime") as batch_op:
        batch_op.alter_column(
            "buffer_snapshot",
            existing_type=sa.JSON(),
            existing_nullable=False,
            server_default=sa.text("'{}'"),
        )


def downgrade() -> None:
    with op.batch_alter_table("human_game_runtime") as batch_op:
        batch_op.alter_column(
            "buffer_snapshot",
            existing_type=sa.JSON(),
            existing_nullable=False,
            server_default=None,
        )
