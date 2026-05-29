"""Add unique constraint to rating_events.

P0 #6: Rating idempotency. Adds a unique constraint on
(game_id, agent_build_id, scope_type, scope_value, public_player_id) to rating_events.

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("rating_events") as batch_op:
        batch_op.add_column(sa.Column("public_player_id", sa.String(), nullable=True))
        batch_op.create_unique_constraint(
            "uq_rating_event_scope",
            ["game_id", "agent_build_id", "scope_type", "scope_value", "public_player_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("rating_events") as batch_op:
        batch_op.drop_constraint("uq_rating_event_scope", type_="unique")
        batch_op.drop_column("public_player_id")
