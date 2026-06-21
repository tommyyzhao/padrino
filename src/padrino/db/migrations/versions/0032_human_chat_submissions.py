"""Add human_chat_submissions buffer-hold table for the chat channel (US-135).

A human submits a public/private chat message over an authenticated POST. The
message enters this buffer *hold* (``status='HELD'``) and is gated by the
block-before-release moderation hook (US-140 lands the verdict) before any
release: on release the raw text is routed to the out-of-band
``human_chat_messages`` sidecar (US-123) and the hold flips to ``'RELEASED'``; a
BLOCK flips to ``'BLOCKED'`` and is never released/chained. ``idempotency_key``
dedupes network retries via a unique key on
``(game_id, public_player_id, phase, idempotency_key)`` so a retry never
double-posts. The chat firewall holds: this text drives no mechanics.

Chains after the current Wave 9 head (0031).

Revision ID: 0032
Revises: 0031
Create Date: 2026-06-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "human_chat_submissions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("game_id", sa.Uuid(), nullable=False),
        sa.Column("public_player_id", sa.String(), nullable=False),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("raw_text", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="HELD"),
        sa.Column("sidecar_sequence", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "game_id",
            "public_player_id",
            "phase",
            "idempotency_key",
            name="uq_human_chat_idempotency",
        ),
    )


def downgrade() -> None:
    op.drop_table("human_chat_submissions")
