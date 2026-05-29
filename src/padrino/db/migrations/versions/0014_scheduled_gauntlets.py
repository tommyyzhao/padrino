"""Add scheduled_gauntlets table for cron-scheduled recurring tournaments.

US-085. A ``scheduled_gauntlets`` row drives the in-process scheduler's
gauntlet job: on each tick the job fires every enabled row whose
``next_run_at`` is due, runs an N-game heterogeneous tournament from the
serialized roster spec, and recomputes ``next_run_at`` from ``schedule_cron``.
``last_run_gauntlet_id`` references the most recent ``gauntlets`` row produced.

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scheduled_gauntlets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("schedule_cron", sa.String(), nullable=False),
        sa.Column("roster_spec_json", sa.JSON(), nullable=False),
        sa.Column("n_games", sa.Integer(), nullable=False),
        sa.Column("cost_cap_usd", sa.Numeric(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_run_gauntlet_id",
            sa.Uuid(),
            sa.ForeignKey("gauntlets.id"),
            nullable=True,
        ),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_scheduled_gauntlets_name"),
    )


def downgrade() -> None:
    op.drop_table("scheduled_gauntlets")
