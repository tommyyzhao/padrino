"""Add per-game lease metadata for benchmark game-grain claims.

Revision ID: 0052
Revises: 0051
Create Date: 2026-06-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GAME_LEASE_INDEX = "ix_games_status_lease_expires_at"


def upgrade() -> None:
    with op.batch_alter_table("games") as batch_op:
        batch_op.add_column(sa.Column("leased_by", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("attempt_count", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("last_error", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("last_error_kind", sa.String(), nullable=True))
        batch_op.create_index(_GAME_LEASE_INDEX, ["status", "lease_expires_at"])


def downgrade() -> None:
    with op.batch_alter_table("games") as batch_op:
        batch_op.drop_index(_GAME_LEASE_INDEX)
        batch_op.drop_column("last_error_kind")
        batch_op.drop_column("last_error")
        batch_op.drop_column("attempt_count")
        batch_op.drop_column("lease_expires_at")
        batch_op.drop_column("leased_by")
