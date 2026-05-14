"""Ratings and rating-event persistence tables.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-14

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ratings",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("league_id", sa.Uuid(), nullable=False),
        sa.Column("agent_build_id", sa.Uuid(), nullable=False),
        sa.Column("scope_type", sa.String(), nullable=False),
        sa.Column("scope_value", sa.String(), nullable=False),
        sa.Column("mu", sa.Numeric(asdecimal=False), nullable=False),
        sa.Column("sigma", sa.Numeric(asdecimal=False), nullable=False),
        sa.Column("conservative_score", sa.Numeric(asdecimal=False), nullable=False),
        sa.Column("games", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["league_id"], ["leagues.id"]),
        sa.ForeignKeyConstraint(["agent_build_id"], ["agent_builds.id"]),
        sa.UniqueConstraint(
            "league_id",
            "agent_build_id",
            "scope_type",
            "scope_value",
            name="uq_rating_scope",
        ),
    )

    op.create_table(
        "rating_events",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("league_id", sa.Uuid(), nullable=False),
        sa.Column("game_id", sa.Uuid(), nullable=False),
        sa.Column("agent_build_id", sa.Uuid(), nullable=False),
        sa.Column("scope_type", sa.String(), nullable=False),
        sa.Column("scope_value", sa.String(), nullable=False),
        sa.Column("before_mu", sa.Numeric(asdecimal=False), nullable=False),
        sa.Column("before_sigma", sa.Numeric(asdecimal=False), nullable=False),
        sa.Column("after_mu", sa.Numeric(asdecimal=False), nullable=False),
        sa.Column("after_sigma", sa.Numeric(asdecimal=False), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["league_id"], ["leagues.id"]),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"]),
        sa.ForeignKeyConstraint(["agent_build_id"], ["agent_builds.id"]),
    )


def downgrade() -> None:
    op.drop_table("rating_events")
    op.drop_table("ratings")
