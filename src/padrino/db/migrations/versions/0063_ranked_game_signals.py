"""Add ranked-game telemetry signals.

Revision ID: 0063
Revises: 0062
Create Date: 2026-06-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0063"
down_revision: str | None = "0062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GAME_INDEX = "uq_ranked_game_signals_game_id"
SOURCE_IP_INDEX = "ix_ranked_game_signals_source_ip_hash"
HUMAN_PRINCIPAL_INDEX = "ix_ranked_game_signals_human_principal_id"


def upgrade() -> None:
    with op.batch_alter_table("games") as batch_op:
        batch_op.add_column(sa.Column("ranked_source_ip_hash", sa.String(), nullable=True))

    op.create_table(
        "ranked_game_signals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("game_id", sa.Uuid(), nullable=False),
        sa.Column("human_principal_id", sa.Uuid(), nullable=False),
        sa.Column("source_ip_hash", sa.String(), nullable=False),
        sa.Column("session_fingerprint_hash", sa.String(), nullable=True),
        sa.Column("winner", sa.String(), nullable=False),
        sa.Column("faction", sa.String(), nullable=False),
        sa.Column("signals", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"]),
        sa.ForeignKeyConstraint(["human_principal_id"], ["principals.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(GAME_INDEX, "ranked_game_signals", ["game_id"], unique=True)
    op.create_index(SOURCE_IP_INDEX, "ranked_game_signals", ["source_ip_hash"])
    op.create_index(HUMAN_PRINCIPAL_INDEX, "ranked_game_signals", ["human_principal_id"])


def downgrade() -> None:
    op.drop_index(HUMAN_PRINCIPAL_INDEX, table_name="ranked_game_signals")
    op.drop_index(SOURCE_IP_INDEX, table_name="ranked_game_signals")
    op.drop_index(GAME_INDEX, table_name="ranked_game_signals")
    op.drop_table("ranked_game_signals")
    with op.batch_alter_table("games") as batch_op:
        batch_op.drop_column("ranked_source_ip_hash")
