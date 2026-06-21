"""Atomic inference-$ reservation slots + releasable admission slots (US-190).

Revision ID: 0044
Revises: 0043
Create Date: 2026-06-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Tie an admission slot to the lobby / member it produced so an abandoned
    # action can release it (the per-day caps count actual games/joins).
    with op.batch_alter_table("human_cost_admissions") as batch_op:
        batch_op.add_column(sa.Column("lobby_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("lobby_member_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("released_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_foreign_key(
            "fk_human_cost_admission_lobby",
            "lobbies",
            ["lobby_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_human_cost_admission_lobby_member",
            "lobby_members",
            ["lobby_member_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.create_table(
        "human_inference_reservations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("scope_key", sa.String(), nullable=False),
        sa.Column("reservation_day", sa.Date(), nullable=False),
        sa.Column("slot_index", sa.Integer(), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lobby_id", sa.Uuid(), nullable=True),
        sa.Column("lobby_member_id", sa.Uuid(), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["lobby_id"], ["lobbies.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["lobby_member_id"], ["lobby_members.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope_key",
            "reservation_day",
            "slot_index",
            name="uq_human_inference_reservation_slot",
        ),
    )


def downgrade() -> None:
    op.drop_table("human_inference_reservations")
    with op.batch_alter_table("human_cost_admissions") as batch_op:
        batch_op.drop_constraint("fk_human_cost_admission_lobby_member", type_="foreignkey")
        batch_op.drop_constraint("fk_human_cost_admission_lobby", type_="foreignkey")
        batch_op.drop_column("released_at")
        batch_op.drop_column("lobby_member_id")
        batch_op.drop_column("lobby_id")
