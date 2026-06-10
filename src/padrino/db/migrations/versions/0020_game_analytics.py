"""Add analytics_aggregates table for per-agent deterministic analytics (US-102).

Stores rolling aggregates of deterministic game analytics keyed by
(ruleset_id, agent_build_id, version).  JSON columns hold serialized
RoleWinRate and SurvivalPoint lists from padrino.analytics.deterministic.

Revision ID: 0020
Revises: 0019
Create Date: 2026-06-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analytics_aggregates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("ruleset_id", sa.String(), nullable=False),
        sa.Column("agent_build_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("games_played", sa.Integer(), nullable=False),
        sa.Column("role_win_rates_json", sa.String(), nullable=False),
        sa.Column("voting_total_votes", sa.Integer(), nullable=False),
        sa.Column("voting_accurate_votes", sa.Integer(), nullable=False),
        sa.Column("survival_curve_json", sa.String(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_build_id"],
            ["agent_builds.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "ruleset_id",
            "agent_build_id",
            "version",
            name="uq_analytics_aggregate",
        ),
    )


def downgrade() -> None:
    op.drop_table("analytics_aggregates")
