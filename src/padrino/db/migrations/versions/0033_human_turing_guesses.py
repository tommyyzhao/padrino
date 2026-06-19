"""Add human_turing_guesses table for the spot-the-AI guess + score (US-144).

After a human game terminates, each human submits ONE guess assigning HUMAN/AI to
every OTHER seat over the existing human channel. The pure scorer computes the
guesser's detection accuracy; the guess + result persist here. Exactly one guess
per ``(game_id, guesser_public_id)`` (a guesser guesses once). There is NO
leaderboard - this row holds one guesser's personal stat only.

Chains after the current Wave 9 head (0032).

Revision ID: 0033
Revises: 0032
Create Date: 2026-06-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "human_turing_guesses",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("game_id", sa.Uuid(), nullable=False),
        sa.Column("guesser_public_id", sa.String(), nullable=False),
        sa.Column("guess", sa.JSON(), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("correct", sa.Integer(), nullable=False),
        sa.Column("accuracy", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "game_id",
            "guesser_public_id",
            name="uq_human_turing_guess",
        ),
    )


def downgrade() -> None:
    op.drop_table("human_turing_guesses")
