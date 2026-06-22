"""Add mirror-pair metadata to games (US-180).

Paired benchmark games are persisted as distinct ``games`` rows that share a
``pair_id`` and differ by ``pair_leg`` (0 = original seat map, 1 = mirrored
seat map). The columns are nullable so every legacy unpaired game remains
byte-identical.

Revision ID: 0048
Revises: 0047
Create Date: 2026-06-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("games") as batch_op:
        batch_op.add_column(sa.Column("pair_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("pair_leg", sa.Integer(), nullable=True))
        batch_op.create_index("ix_games_pair_id", ["pair_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("games") as batch_op:
        batch_op.drop_index("ix_games_pair_id")
        batch_op.drop_column("pair_leg")
        batch_op.drop_column("pair_id")
