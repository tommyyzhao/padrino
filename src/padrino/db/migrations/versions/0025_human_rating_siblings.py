"""Dormant sibling human-rating tables + League discriminator (US-125).

Human games must NEVER touch the sacred scientific benchmark ELO. The human
ELO schema must exist (dormant) so future activation is a flag-flip, not a
migration. This revision:

* adds ``leagues.kind`` (NOT NULL, server_default ``SCIENTIFIC``) so scientific
  vs human leagues are queryable — every existing league row stays byte-identical
  (defaults to ``SCIENTIFIC``);
* creates the sibling tables ``human_rating`` and ``human_rating_event``,
  mirroring the ``ratings`` / ``rating_events`` shape but keyed by
  ``human_player_id``. They are NEVER written in v1.

The scientific ``ratings`` / ``rating_events`` tables are untouched.

Chains after US-123's head (0024).

Revision ID: 0025
Revises: 0024
Create Date: 2026-06-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("leagues") as batch_op:
        batch_op.add_column(
            sa.Column(
                "kind",
                sa.String(),
                nullable=False,
                server_default="SCIENTIFIC",
            )
        )

    op.create_table(
        "human_rating",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("league_id", sa.Uuid(), nullable=False),
        sa.Column("human_player_id", sa.String(), nullable=False),
        sa.Column("scope_type", sa.String(), nullable=False),
        sa.Column("scope_value", sa.String(), nullable=False),
        sa.Column("mu", sa.Numeric(), nullable=False),
        sa.Column("sigma", sa.Numeric(), nullable=False),
        sa.Column("conservative_score", sa.Numeric(), nullable=False),
        sa.Column("games", sa.Integer(), nullable=False),
        sa.Column("last_game_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["league_id"], ["leagues.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "league_id",
            "human_player_id",
            "scope_type",
            "scope_value",
            name="uq_human_rating_scope",
        ),
    )

    op.create_table(
        "human_rating_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("league_id", sa.Uuid(), nullable=False),
        sa.Column("game_id", sa.Uuid(), nullable=False),
        sa.Column("human_player_id", sa.String(), nullable=False),
        sa.Column("public_player_id", sa.String(), nullable=True),
        sa.Column("scope_type", sa.String(), nullable=False),
        sa.Column("scope_value", sa.String(), nullable=False),
        sa.Column("before_mu", sa.Numeric(), nullable=False),
        sa.Column("before_sigma", sa.Numeric(), nullable=False),
        sa.Column("after_mu", sa.Numeric(), nullable=False),
        sa.Column("after_sigma", sa.Numeric(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["league_id"], ["leagues.id"]),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "game_id",
            "human_player_id",
            "scope_type",
            "scope_value",
            name="uq_human_rating_event_scope",
        ),
    )


def downgrade() -> None:
    op.drop_table("human_rating_event")
    op.drop_table("human_rating")
    with op.batch_alter_table("leagues") as batch_op:
        batch_op.drop_column("kind")
