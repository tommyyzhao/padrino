"""Begin the Wave 9 human-multiplayer schema chain on game_seats (US-121).

A game seat must be occupiable by a human OR an agent build OR an AI that took
over a human seat, so humans and AIs coexist in one game without faking an agent
build for a human or polluting agent_build-keyed analytics:

- ``agent_build_id`` becomes nullable (a human seat has no agent build).
- ``seat_kind`` ('AI' | 'HUMAN' | 'AI_TAKEOVER', NOT NULL, server_default 'AI')
  so every existing AI-only seat is byte-identical after the upgrade.
- ``taken_over_at_phase`` / ``takeover_agent_build_id`` record AI takeover
  provenance (both nullable).

This is the FIRST Wave 9 schema migration; every later v9 schema story chains
off it. ``occupant_principal_id`` (FK principals) is deliberately deferred to
US-127 to avoid a forward dependency on the principals table.

Revision ID: 0023
Revises: 0022
Create Date: 2026-06-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # agent_build_id must become nullable for human seats. SQLite cannot ALTER a
    # column's nullability in place, so use a batch (table-rebuild) context that
    # is a no-op rewrite on Postgres' native ALTER.
    with op.batch_alter_table("game_seats") as batch_op:
        batch_op.alter_column(
            "agent_build_id",
            existing_type=sa.Uuid(),
            nullable=True,
        )
        batch_op.add_column(
            sa.Column(
                "seat_kind",
                sa.String(),
                nullable=False,
                server_default="AI",
            )
        )
        batch_op.add_column(sa.Column("taken_over_at_phase", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("takeover_agent_build_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_game_seats_takeover_agent_build_id",
            "agent_builds",
            ["takeover_agent_build_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("game_seats") as batch_op:
        batch_op.drop_constraint("fk_game_seats_takeover_agent_build_id", type_="foreignkey")
        batch_op.drop_column("takeover_agent_build_id")
        batch_op.drop_column("taken_over_at_phase")
        batch_op.drop_column("seat_kind")
        batch_op.alter_column(
            "agent_build_id",
            existing_type=sa.Uuid(),
            nullable=False,
        )
