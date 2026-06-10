"""Add judge_enrichment_cards table for per-agent-role LLM judge trend aggregates (US-105).

Stores average judge dimension scores (persuasion, deception, logical consistency,
social heuristics) keyed by (agent_build_id, role, ruleset_id), clearly separate
from the Rating and RatingEvent tables.

Revision ID: 0021
Revises: 0020
Create Date: 2026-06-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "judge_enrichment_cards",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("agent_build_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("ruleset_id", sa.String(), nullable=False),
        sa.Column("games_count", sa.Integer(), nullable=False),
        sa.Column("avg_persuasion", sa.Numeric(), nullable=False),
        sa.Column("avg_deception", sa.Numeric(), nullable=False),
        sa.Column("avg_logical_consistency", sa.Numeric(), nullable=False),
        sa.Column("avg_social_heuristics", sa.Numeric(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["agent_build_id"],
            ["agent_builds.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "agent_build_id",
            "role",
            "ruleset_id",
            name="uq_judge_enrichment_card",
        ),
    )


def downgrade() -> None:
    op.drop_table("judge_enrichment_cards")
