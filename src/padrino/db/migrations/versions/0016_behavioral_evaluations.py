"""Add behavioral_evaluations table for post-game LLM behavioral reviews.

Wave 6. Adds a `behavioral_evaluations` table to store LLM judge evaluations
across four dimensions (Persuasion, Deception, Logic, Social) plus feedback.

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "behavioral_evaluations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "game_id", sa.Uuid(), sa.ForeignKey("games.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "agent_build_id",
            sa.Uuid(),
            sa.ForeignKey("agent_builds.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("public_player_id", sa.String(), nullable=False),
        sa.Column("persuasion_score", sa.Integer(), nullable=False),
        sa.Column("deception_score", sa.Integer(), nullable=False),
        sa.Column("logical_consistency_score", sa.Integer(), nullable=False),
        sa.Column("social_heuristics_score", sa.Integer(), nullable=False),
        sa.Column("written_feedback", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_id", "public_player_id", name="uq_behavioral_eval_seat"),
    )


def downgrade() -> None:
    op.drop_table("behavioral_evaluations")
