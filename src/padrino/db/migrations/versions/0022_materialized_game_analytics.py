"""Add materialized_game_analytics table for per-game recap analytics (US-120).

Stores the full deterministic per-game analytics + claim analysis as a JSON
blob keyed by game_id, computed once when a game becomes RECENT so the public
recap page does not re-derive the full event log on every request.

Revision ID: 0022
Revises: 0021
Create Date: 2026-06-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "materialized_game_analytics",
        sa.Column("game_id", sa.Uuid(), nullable=False),
        sa.Column("ruleset_id", sa.String(), nullable=False),
        sa.Column("analytics_json", sa.String(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("game_id"),
    )


def downgrade() -> None:
    op.drop_table("materialized_game_analytics")
