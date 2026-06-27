"""Create ranked game exclusions and rollback audit rows.

Revision ID: 0061
Revises: 0060
Create Date: 2026-06-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0061"
down_revision: str | None = "0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXCLUSION_UNIQUE_INDEX = "uq_ranked_game_exclusions_principal_game"
EXCLUSION_GAME_INDEX = "ix_ranked_game_exclusions_game_id"
ROLLBACK_AUDIT_UNIQUE_INDEX = "uq_ranked_rating_rollback_audit_principal_hash"


def upgrade() -> None:
    op.create_table(
        "ranked_game_exclusions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("human_player_id", sa.String(), nullable=False),
        sa.Column("game_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["principal_id"], ["principals.id"]),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        EXCLUSION_UNIQUE_INDEX,
        "ranked_game_exclusions",
        ["principal_id", "game_id"],
        unique=True,
    )
    op.create_index(
        EXCLUSION_GAME_INDEX,
        "ranked_game_exclusions",
        ["game_id"],
    )

    op.create_table(
        "ranked_rating_rollback_audit",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("human_player_id", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("excluded_game_ids", sa.JSON(), nullable=False),
        sa.Column("exclusion_hash", sa.String(), nullable=False),
        sa.Column("created_by", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["principal_id"], ["principals.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        ROLLBACK_AUDIT_UNIQUE_INDEX,
        "ranked_rating_rollback_audit",
        ["principal_id", "exclusion_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        ROLLBACK_AUDIT_UNIQUE_INDEX,
        table_name="ranked_rating_rollback_audit",
    )
    op.drop_table("ranked_rating_rollback_audit")
    op.drop_index(EXCLUSION_GAME_INDEX, table_name="ranked_game_exclusions")
    op.drop_index(EXCLUSION_UNIQUE_INDEX, table_name="ranked_game_exclusions")
    op.drop_table("ranked_game_exclusions")
