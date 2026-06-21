"""Out-of-band human-chat sidecar, off the hash chain (US-123).

Human free-text chat is PII and must be GDPR-erasable. Raw text inside a
hash-chained ``game_events`` row would make erasure impossible without breaking
deterministic replay, so the raw text lives in this sidecar and the paired core
event carries only an opaque ``content_ref`` (sha256). Redacting a sidecar row
never touches ``game_events``, so the hash chain still verifies.

Chains after US-121's Wave 9 head (0023).

Revision ID: 0024
Revises: 0023
Create Date: 2026-06-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "human_chat_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("game_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("public_player_id", sa.String(), nullable=False),
        sa.Column("raw_text", sa.String(), nullable=True),
        sa.Column("cleaned_text", sa.String(), nullable=True),
        sa.Column(
            "redacted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("game_id", "sequence", name="uq_human_chat_message_sequence"),
    )


def downgrade() -> None:
    op.drop_table("human_chat_messages")
