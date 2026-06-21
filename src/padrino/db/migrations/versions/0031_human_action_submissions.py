"""Add human_action_submissions table for the authenticated action channel (US-134).

A human submits a structured ``Action`` (vote / protect / investigate / etc.)
over an authenticated POST channel. Validated submissions are buffered here so
the human-aware tick can later resolve the seat's turn. ``idempotency_key``
dedupes network retries via a unique key on
``(game_id, public_player_id, phase, idempotency_key)`` so a retried POST never
double-votes. Raw chat text never lives here.

Chains after the current Wave 9 head (0030).

Revision ID: 0031
Revises: 0030
Create Date: 2026-06-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "human_action_submissions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("game_id", sa.Uuid(), nullable=False),
        sa.Column("public_player_id", sa.String(), nullable=False),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("action_type", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "game_id",
            "public_player_id",
            "phase",
            "idempotency_key",
            name="uq_human_action_idempotency",
        ),
    )


def downgrade() -> None:
    op.drop_table("human_action_submissions")
