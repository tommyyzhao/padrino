"""Add generic budget reservation slots.

Revision ID: 0054
Revises: 0053
Create Date: 2026-06-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "budget_reservation_slots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("scope_key", sa.String(), nullable=False),
        sa.Column("slot_index", sa.Integer(), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("binding_key", sa.String(), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope_key",
            "slot_index",
            name="uq_budget_reservation_slot",
        ),
    )


def downgrade() -> None:
    op.drop_table("budget_reservation_slots")
