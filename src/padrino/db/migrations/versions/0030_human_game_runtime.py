"""Add human_game_runtime table for durable, rehydratable human games (US-131).

A human-lane game can last minutes to hours, so a process restart must not lose
it. ``human_game_runtime`` holds ONLY the impure live runner scaffolding (current
phase, the phase wall-clock deadline, and an opaque buffer snapshot of in-flight
human submissions), keyed one-to-one by ``game_id``. The deterministic core game
state is never stored here — it is always reconstructed by replaying the
hash-chained ``game_events`` log.

Chains after the current Wave 9 head (0029). US-121 (the named dependency) is
0023; the chain has since advanced, so this revises 0029 to stay linear.

Revision ID: 0030
Revises: 0029
Create Date: 2026-06-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "human_game_runtime",
        sa.Column("game_id", sa.Uuid(), nullable=False),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("buffer_snapshot", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("game_id"),
    )


def downgrade() -> None:
    op.drop_table("human_game_runtime")
