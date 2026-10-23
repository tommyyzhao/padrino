"""Add append-only promotional-credit and completion-reserve ledgers.

Revision ID: 0072
Revises: 0071
Create Date: 2026-07-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0072"
down_revision: str | None = "0071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Persist integer-micro wallet movements, holds, and operator subsidies."""
    op.create_table(
        "promotional_credit_reservations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("lobby_id", sa.Uuid(), nullable=True),
        sa.Column("game_id", sa.Uuid(), nullable=True),
        sa.Column("estimate_hash", sa.String(length=64), nullable=False),
        sa.Column("catalog_hash", sa.String(length=64), nullable=False),
        sa.Column("rules_hash", sa.String(length=64), nullable=False),
        sa.Column("reserved_micros", sa.Integer(), nullable=False),
        sa.Column("consumed_micros", sa.Integer(), nullable=False),
        sa.Column("released_micros", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'SETTLED', 'CANCELLED')",
            name="ck_promotional_credit_reservations_status",
        ),
        sa.CheckConstraint(
            "reserved_micros >= 0 AND consumed_micros >= 0 AND released_micros >= 0",
            name="ck_promotional_credit_reservations_non_negative_micros",
        ),
        sa.CheckConstraint(
            "consumed_micros + released_micros <= reserved_micros",
            name="ck_promotional_credit_reservations_accounted_micros",
        ),
        sa.CheckConstraint(
            "length(estimate_hash) = 64",
            name="ck_promotional_credit_reservations_estimate_hash_len",
        ),
        sa.CheckConstraint(
            "length(catalog_hash) = 64",
            name="ck_promotional_credit_reservations_catalog_hash_len",
        ),
        sa.CheckConstraint(
            "length(rules_hash) = 64",
            name="ck_promotional_credit_reservations_rules_hash_len",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name="fk_promotional_credit_reservations_principal_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["lobby_id"],
            ["lobbies.id"],
            name="fk_promotional_credit_reservations_lobby_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
            name="fk_promotional_credit_reservations_game_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_promotional_credit_reservations"),
    )
    op.create_index(
        "uq_promotional_credit_reservations_idempotency_key",
        "promotional_credit_reservations",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index(
        "uq_promotional_credit_reservations_active_game",
        "promotional_credit_reservations",
        ["game_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE' AND game_id IS NOT NULL"),
        sqlite_where=sa.text("status = 'ACTIVE' AND game_id IS NOT NULL"),
    )
    op.create_index(
        "uq_promotional_credit_reservations_active_lobby",
        "promotional_credit_reservations",
        ["lobby_id"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE' AND lobby_id IS NOT NULL"),
        sqlite_where=sa.text("status = 'ACTIVE' AND lobby_id IS NOT NULL"),
    )
    op.create_index(
        "ix_promotional_credit_reservations_principal_status",
        "promotional_credit_reservations",
        ["principal_id", "status"],
    )

    op.create_table(
        "promotional_credit_ledger_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("reservation_id", sa.Uuid(), nullable=True),
        sa.Column("game_id", sa.Uuid(), nullable=True),
        sa.Column("entry_type", sa.String(), nullable=False),
        sa.Column("amount_micros", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "entry_type IN ('GRANT', 'RESERVATION', 'CHARGE', 'RELEASE', 'ADJUSTMENT')",
            name="ck_promotional_credit_ledger_entries_type",
        ),
        sa.CheckConstraint(
            "(entry_type IN ('GRANT', 'RELEASE') AND amount_micros > 0) "
            "OR (entry_type IN ('RESERVATION', 'CHARGE') AND amount_micros < 0) "
            "OR (entry_type = 'ADJUSTMENT' AND amount_micros != 0)",
            name="ck_promotional_credit_ledger_entries_amount_sign",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["principals.id"],
            name="fk_promotional_credit_ledger_entries_principal_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["promotional_credit_reservations.id"],
            name="fk_promotional_credit_ledger_entries_reservation_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
            name="fk_promotional_credit_ledger_entries_game_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_promotional_credit_ledger_entries"),
    )
    op.create_index(
        "uq_promotional_credit_ledger_entries_idempotency_key",
        "promotional_credit_ledger_entries",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index(
        "uq_promotional_credit_ledger_entries_promo_grant_principal",
        "promotional_credit_ledger_entries",
        ["principal_id"],
        unique=True,
        postgresql_where=sa.text("entry_type = 'GRANT'"),
        sqlite_where=sa.text("entry_type = 'GRANT'"),
    )
    op.create_index(
        "ix_promotional_credit_ledger_entries_principal_created_at",
        "promotional_credit_ledger_entries",
        ["principal_id", "created_at"],
    )
    op.create_index(
        "ix_promotional_credit_ledger_entries_reservation_id",
        "promotional_credit_ledger_entries",
        ["reservation_id"],
    )

    op.create_table(
        "operator_completion_reserve_ledger_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("game_id", sa.Uuid(), nullable=True),
        sa.Column("entry_type", sa.String(), nullable=False),
        sa.Column("amount_micros", sa.Integer(), nullable=False),
        sa.Column("estimate_miss_micros", sa.Integer(), nullable=False),
        sa.Column("model_slug", sa.String(), nullable=True),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "entry_type IN ('ALLOCATION', 'CHARGE', 'ADJUSTMENT')",
            name="ck_operator_completion_reserve_ledger_entries_type",
        ),
        sa.CheckConstraint(
            "(entry_type = 'ALLOCATION' AND amount_micros > 0) "
            "OR (entry_type = 'CHARGE' AND amount_micros < 0) "
            "OR (entry_type = 'ADJUSTMENT' AND amount_micros != 0)",
            name="ck_operator_completion_reserve_ledger_entries_amount_sign",
        ),
        sa.CheckConstraint(
            "estimate_miss_micros >= 0",
            name="ck_operator_completion_reserve_ledger_entries_non_negative_miss",
        ),
        sa.CheckConstraint(
            "entry_type != 'CHARGE' OR "
            "(game_id IS NOT NULL AND model_slug IS NOT NULL AND estimate_miss_micros > 0)",
            name="ck_operator_completion_reserve_ledger_entries_charge_attrib",
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
            name="fk_operator_completion_reserve_ledger_entries_game_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_operator_completion_reserve_ledger_entries"),
    )
    op.create_index(
        "uq_operator_completion_reserve_ledger_entries_idempotency_key",
        "operator_completion_reserve_ledger_entries",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_operator_completion_reserve_ledger_entries_game_id",
        "operator_completion_reserve_ledger_entries",
        ["game_id"],
    )


def downgrade() -> None:
    """Remove the additive accounting tables in dependency order."""
    op.drop_index(
        "ix_operator_completion_reserve_ledger_entries_game_id",
        table_name="operator_completion_reserve_ledger_entries",
    )
    op.drop_index(
        "uq_operator_completion_reserve_ledger_entries_idempotency_key",
        table_name="operator_completion_reserve_ledger_entries",
    )
    op.drop_table("operator_completion_reserve_ledger_entries")

    op.drop_index(
        "ix_promotional_credit_ledger_entries_reservation_id",
        table_name="promotional_credit_ledger_entries",
    )
    op.drop_index(
        "ix_promotional_credit_ledger_entries_principal_created_at",
        table_name="promotional_credit_ledger_entries",
    )
    op.drop_index(
        "uq_promotional_credit_ledger_entries_promo_grant_principal",
        table_name="promotional_credit_ledger_entries",
    )
    op.drop_index(
        "uq_promotional_credit_ledger_entries_idempotency_key",
        table_name="promotional_credit_ledger_entries",
    )
    op.drop_table("promotional_credit_ledger_entries")

    op.drop_index(
        "ix_promotional_credit_reservations_principal_status",
        table_name="promotional_credit_reservations",
    )
    op.drop_index(
        "uq_promotional_credit_reservations_active_lobby",
        table_name="promotional_credit_reservations",
    )
    op.drop_index(
        "uq_promotional_credit_reservations_active_game",
        table_name="promotional_credit_reservations",
    )
    op.drop_index(
        "uq_promotional_credit_reservations_idempotency_key",
        table_name="promotional_credit_reservations",
    )
    op.drop_table("promotional_credit_reservations")
