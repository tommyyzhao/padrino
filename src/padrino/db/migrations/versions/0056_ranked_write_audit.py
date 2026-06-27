"""Add ranked human-rating write provenance audit rows.

Revision ID: 0056
Revises: 0055
Create Date: 2026-06-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0056"
down_revision: str | None = "0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APPLIED_INDEX = "uq_ranked_write_audit_applied_game_human"
_REFUSED_INDEX = "uq_ranked_write_audit_refused_game_reason"


def upgrade() -> None:
    op.create_table(
        "ranked_write_audit",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("game_id", sa.Uuid(), nullable=False),
        sa.Column("human_player_id", sa.String(), nullable=True),
        sa.Column("league_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("flag_enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN ('APPLIED', 'REFUSED')",
            name="ck_ranked_write_audit_decision",
        ),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"]),
        sa.ForeignKeyConstraint(["league_id"], ["leagues.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        _APPLIED_INDEX,
        "ranked_write_audit",
        ["game_id", "human_player_id"],
        unique=True,
        sqlite_where=sa.text("decision = 'APPLIED' AND human_player_id IS NOT NULL"),
        postgresql_where=sa.text("decision = 'APPLIED' AND human_player_id IS NOT NULL"),
    )
    op.create_index(
        _REFUSED_INDEX,
        "ranked_write_audit",
        ["game_id", "reason"],
        unique=True,
        sqlite_where=sa.text("decision = 'REFUSED' AND human_player_id IS NULL"),
        postgresql_where=sa.text("decision = 'REFUSED' AND human_player_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(_REFUSED_INDEX, table_name="ranked_write_audit")
    op.drop_index(_APPLIED_INDEX, table_name="ranked_write_audit")
    op.drop_table("ranked_write_audit")
