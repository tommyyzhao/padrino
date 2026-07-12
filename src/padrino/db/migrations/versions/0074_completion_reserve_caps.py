"""Freeze completion-reserve policy and serialize cap settlement.

Revision ID: 0074
Revises: 0073
Create Date: 2026-07-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0074"
down_revision: str | None = "0073"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Persist frozen per-game policy and idempotent cap accounting facts."""
    with op.batch_alter_table("promotional_credit_reservations") as batch:
        batch.add_column(sa.Column("completion_reserve_per_game_cap_micros", sa.Integer()))
        batch.add_column(sa.Column("completion_reserve_unfunded_policy", sa.String()))
        batch.create_check_constraint(
            "ck_promotional_credit_reservations_completion_cap_non_negative",
            "completion_reserve_per_game_cap_micros IS NULL "
            "OR completion_reserve_per_game_cap_micros >= 0",
        )
        batch.create_check_constraint(
            "ck_promotional_credit_reservations_completion_policy_pair",
            "(completion_reserve_per_game_cap_micros IS NULL "
            "AND completion_reserve_unfunded_policy IS NULL) "
            "OR (completion_reserve_per_game_cap_micros IS NOT NULL "
            "AND completion_reserve_unfunded_policy IN ('COERCE', 'NO_CONTEST'))",
        )

    op.create_table(
        "operator_completion_reserve_cursors",
        sa.Column("scope_key", sa.String(), nullable=False),
        sa.Column("charged_micros", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "charged_micros >= 0",
            name="ck_operator_completion_reserve_cursors_non_negative_charged",
        ),
        sa.PrimaryKeyConstraint("scope_key", name="pk_operator_completion_reserve_cursors"),
    )
    # Existing estimate-miss charges remain the monetary authority when this
    # migration lands. Seed the singleton lock cursor from that ledger so the
    # first post-upgrade settlement cannot accidentally ignore prior spend.
    op.execute(
        sa.text(
            "INSERT INTO operator_completion_reserve_cursors "
            "(scope_key, charged_micros, updated_at) "
            "SELECT 'global', COALESCE(SUM(-amount_micros), 0), CURRENT_TIMESTAMP "
            "FROM operator_completion_reserve_ledger_entries "
            "WHERE entry_type = 'CHARGE'"
        )
    )
    op.create_table(
        "operator_completion_reserve_settlements",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("game_id", sa.Uuid(), nullable=False),
        sa.Column("reservation_id", sa.Uuid(), nullable=False),
        sa.Column("model_slug", sa.String(), nullable=False),
        sa.Column("call_cost_micros", sa.Integer(), nullable=False),
        sa.Column("promotional_charge_micros", sa.Integer(), nullable=False),
        sa.Column("completion_reserve_charge_micros", sa.Integer(), nullable=False),
        sa.Column("unfunded_micros", sa.Integer(), nullable=False),
        sa.Column("unfunded_policy", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "call_cost_micros >= 0 AND promotional_charge_micros >= 0 "
            "AND completion_reserve_charge_micros >= 0 AND unfunded_micros >= 0",
            name="ck_operator_completion_reserve_settlements_non_negative_micros",
        ),
        sa.CheckConstraint(
            "promotional_charge_micros + completion_reserve_charge_micros + unfunded_micros "
            "= call_cost_micros",
            name="ck_operator_completion_reserve_settlements_conservation",
        ),
        sa.CheckConstraint(
            "unfunded_policy IN ('COERCE', 'NO_CONTEST')",
            name="ck_operator_completion_reserve_settlements_unfunded_policy",
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["games.id"],
            name="fk_operator_completion_reserve_settlements_game_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["promotional_credit_reservations.id"],
            name="fk_operator_completion_reserve_settlements_reservation_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_operator_completion_reserve_settlements"),
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_operator_completion_reserve_settlements_idempotency_key",
        ),
    )
    op.create_index(
        "ix_operator_completion_reserve_settlements_game_id",
        "operator_completion_reserve_settlements",
        ["game_id"],
    )


def downgrade() -> None:
    """Remove completion-reserve settlement state without touching ledgers."""
    op.drop_index(
        "ix_operator_completion_reserve_settlements_game_id",
        table_name="operator_completion_reserve_settlements",
    )
    op.drop_table("operator_completion_reserve_settlements")
    op.drop_table("operator_completion_reserve_cursors")
    with op.batch_alter_table("promotional_credit_reservations") as batch:
        batch.drop_constraint(
            "ck_promotional_credit_reservations_completion_policy_pair",
            type_="check",
        )
        batch.drop_constraint(
            "ck_promotional_credit_reservations_completion_cap_non_negative",
            type_="check",
        )
        batch.drop_column("completion_reserve_unfunded_policy")
        batch.drop_column("completion_reserve_per_game_cap_micros")
