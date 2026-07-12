"""Bind durable funding settlements to billable LLM calls.

Revision ID: 0075
Revises: 0074
Create Date: 2026-07-12
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0075"
down_revision: str | None = "0074"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add exact call-charge keys and optional reserve-settlement provenance."""
    with op.batch_alter_table("llm_calls") as batch:
        batch.add_column(sa.Column("charge_idempotency_key", sa.String(), nullable=True))
        batch.add_column(sa.Column("charge_amount_micros", sa.Integer(), nullable=True))
        batch.create_check_constraint(
            "ck_llm_calls_charge_pair",
            "(charge_idempotency_key IS NULL AND charge_amount_micros IS NULL) "
            "OR (charge_idempotency_key IS NOT NULL AND charge_amount_micros >= 0)",
        )
    op.create_index(
        "uq_llm_calls_charge_idempotency_key",
        "llm_calls",
        ["charge_idempotency_key"],
        unique=True,
        postgresql_where=sa.text("charge_idempotency_key IS NOT NULL"),
        sqlite_where=sa.text("charge_idempotency_key IS NOT NULL"),
    )

    with op.batch_alter_table("operator_completion_reserve_settlements") as batch:
        batch.add_column(sa.Column("llm_call_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_operator_completion_reserve_settlements_llm_call_id",
            "llm_calls",
            ["llm_call_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.create_index(
        "uq_operator_completion_reserve_settlements_llm_call",
        "operator_completion_reserve_settlements",
        ["llm_call_id"],
        unique=True,
        postgresql_where=sa.text("llm_call_id IS NOT NULL"),
        sqlite_where=sa.text("llm_call_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Remove call-settlement provenance in reverse dependency order."""
    op.drop_index(
        "uq_operator_completion_reserve_settlements_llm_call",
        table_name="operator_completion_reserve_settlements",
    )
    with op.batch_alter_table("operator_completion_reserve_settlements") as batch:
        batch.drop_constraint(
            "fk_operator_completion_reserve_settlements_llm_call_id",
            type_="foreignkey",
        )
        batch.drop_column("llm_call_id")

    op.drop_index("uq_llm_calls_charge_idempotency_key", table_name="llm_calls")
    with op.batch_alter_table("llm_calls") as batch:
        batch.drop_constraint("ck_llm_calls_charge_pair", type_="check")
        batch.drop_column("charge_amount_micros")
        batch.drop_column("charge_idempotency_key")
