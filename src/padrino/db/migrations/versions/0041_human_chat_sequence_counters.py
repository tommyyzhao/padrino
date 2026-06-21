"""Add atomic human-chat sidecar sequence counters (US-166).

Revision ID: 0041
Revises: 0040
Create Date: 2026-06-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "human_chat_sequence_counters",
        sa.Column("game_id", sa.Uuid(), nullable=False),
        sa.Column("next_sequence", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("game_id"),
    )


def downgrade() -> None:
    op.drop_table("human_chat_sequence_counters")
