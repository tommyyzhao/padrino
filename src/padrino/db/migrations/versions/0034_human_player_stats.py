"""Add human_player_stats table for per-human deterministic play history (US-145).

A signed-in player's play history yields deterministic stats (win rate by
role/faction, survival, voting accuracy, detection accuracy) keyed by
``(ruleset_id, principal_id)``.  Materialized ONLY from human-lane games; never
written for scientific leagues, and there is NO leaderboard or ELO in v1.

Chains after the current Wave 9 head (0033).

Revision ID: 0034
Revises: 0033
Create Date: 2026-06-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "human_player_stats",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ruleset_id", sa.String(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("games", sa.Integer(), nullable=False),
        sa.Column("wins", sa.Integer(), nullable=False),
        sa.Column("draws", sa.Integer(), nullable=False),
        sa.Column("losses", sa.Integer(), nullable=False),
        sa.Column("role_win_rates_json", sa.String(), nullable=False),
        sa.Column("faction_win_rates_json", sa.String(), nullable=False),
        sa.Column("survived_games", sa.Integer(), nullable=False),
        sa.Column("voting_total_votes", sa.Integer(), nullable=False),
        sa.Column("voting_accurate_votes", sa.Integer(), nullable=False),
        sa.Column("detection_total", sa.Integer(), nullable=False),
        sa.Column("detection_accurate", sa.Integer(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["principal_id"], ["principals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ruleset_id",
            "principal_id",
            name="uq_human_player_stats",
        ),
    )


def downgrade() -> None:
    op.drop_table("human_player_stats")
