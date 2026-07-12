"""Persist lobby and game rules snapshots.

Revision ID: 0065
Revises: 0064
Create Date: 2026-07-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0065"
down_revision: str | None = "0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GAMES_PAIR_CHECK = "ck_games_rules_snapshot_pair"
GAMES_HASH_LEN_CHECK = "ck_games_rules_snapshot_hash_len"
LOBBIES_PAIR_CHECK = "ck_lobbies_rules_snapshot_pair"
LOBBIES_HASH_LEN_CHECK = "ck_lobbies_rules_snapshot_hash_len"


def upgrade() -> None:
    with op.batch_alter_table("games") as batch_op:
        batch_op.add_column(
            sa.Column("rules_snapshot_json", sa.JSON(none_as_null=True), nullable=True)
        )
        batch_op.add_column(sa.Column("rules_snapshot_hash", sa.String(), nullable=True))
        batch_op.create_check_constraint(
            GAMES_PAIR_CHECK,
            "(rules_snapshot_json IS NULL AND rules_snapshot_hash IS NULL) "
            "OR (rules_snapshot_json IS NOT NULL AND rules_snapshot_hash IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            GAMES_HASH_LEN_CHECK,
            "rules_snapshot_hash IS NULL OR length(rules_snapshot_hash) = 64",
        )

    with op.batch_alter_table("lobbies") as batch_op:
        batch_op.add_column(
            sa.Column("rules_snapshot_json", sa.JSON(none_as_null=True), nullable=True)
        )
        batch_op.add_column(sa.Column("rules_snapshot_hash", sa.String(), nullable=True))
        batch_op.create_check_constraint(
            LOBBIES_PAIR_CHECK,
            "(rules_snapshot_json IS NULL AND rules_snapshot_hash IS NULL) "
            "OR (rules_snapshot_json IS NOT NULL AND rules_snapshot_hash IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            LOBBIES_HASH_LEN_CHECK,
            "rules_snapshot_hash IS NULL OR length(rules_snapshot_hash) = 64",
        )


def downgrade() -> None:
    with op.batch_alter_table("lobbies") as batch_op:
        batch_op.drop_constraint(LOBBIES_HASH_LEN_CHECK, type_="check")
        batch_op.drop_constraint(LOBBIES_PAIR_CHECK, type_="check")
        batch_op.drop_column("rules_snapshot_hash")
        batch_op.drop_column("rules_snapshot_json")

    with op.batch_alter_table("games") as batch_op:
        batch_op.drop_constraint(GAMES_HASH_LEN_CHECK, type_="check")
        batch_op.drop_constraint(GAMES_PAIR_CHECK, type_="check")
        batch_op.drop_column("rules_snapshot_hash")
        batch_op.drop_column("rules_snapshot_json")
