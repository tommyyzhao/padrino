"""Game.terminal_result becomes JSON; terminal_reason column dropped.

US-049 makes the runner persist a single canonical terminal-result payload
on the ``games`` row (``{winner, reason, day_terminated}``) atomically with
the ``GameTerminated`` event row, so downstream code can filter on
``Game.status='COMPLETED'`` instead of scanning ``game_events``.

Revision ID: 0004
Revises: 0003
Create Date: 2026-05-14

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("games") as batch:
        batch.drop_column("terminal_reason")
        batch.alter_column(
            "terminal_result",
            existing_type=sa.String(),
            type_=sa.JSON(),
            existing_nullable=True,
            postgresql_using="terminal_result::json",
        )


def downgrade() -> None:
    with op.batch_alter_table("games") as batch:
        batch.alter_column(
            "terminal_result",
            existing_type=sa.JSON(),
            type_=sa.String(),
            existing_nullable=True,
        )
        batch.add_column(sa.Column("terminal_reason", sa.String(), nullable=True))
