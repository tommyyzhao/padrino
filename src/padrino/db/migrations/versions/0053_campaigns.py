"""Persist benchmark campaigns and pairing-matrix ledger rows.

Revision ID: 0053
Revises: 0052
Create Date: 2026-06-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GAUNTLET_CAMPAIGN_INDEX = "ix_gauntlets_campaign_id"
_GAUNTLET_CAMPAIGN_FK = "fk_gauntlets_campaign_id_campaigns"


def upgrade() -> None:
    op.create_table(
        "campaigns",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("campaign_seed", sa.String(), nullable=False),
        sa.Column("ruleset_id", sa.String(), nullable=False),
        sa.Column("league_id", sa.Uuid(), nullable=False),
        sa.Column("format", sa.String(), nullable=False),
        sa.Column("player_count", sa.Integer(), nullable=False),
        sa.Column("per_model_game_target", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("leased_by", sa.String(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sigma_target", sa.Numeric(asdecimal=False), nullable=False),
        sa.Column("rank_stability_k", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["league_id"], ["leagues.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "campaign_pairings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("cell_index", sa.Integer(), nullable=False),
        sa.Column("roster_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("gauntlet_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"]),
        sa.ForeignKeyConstraint(["gauntlet_id"], ["gauntlets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "cell_index", name="uq_campaign_pairing_cell"),
    )

    with op.batch_alter_table("gauntlets") as batch_op:
        batch_op.add_column(sa.Column("campaign_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            _GAUNTLET_CAMPAIGN_FK,
            "campaigns",
            ["campaign_id"],
            ["id"],
        )
        batch_op.create_index(_GAUNTLET_CAMPAIGN_INDEX, ["campaign_id"])


def downgrade() -> None:
    op.drop_table("campaign_pairings")

    with op.batch_alter_table("gauntlets") as batch_op:
        batch_op.drop_index(_GAUNTLET_CAMPAIGN_INDEX)
        batch_op.drop_constraint(_GAUNTLET_CAMPAIGN_FK, type_="foreignkey")
        batch_op.drop_column("campaign_id")

    op.drop_table("campaigns")
