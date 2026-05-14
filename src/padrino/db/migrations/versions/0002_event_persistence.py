"""Event and LLM-call persistence tables.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-14

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "game_events",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("game_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("visibility", sa.String(), nullable=False),
        sa.Column("actor_player_id", sa.String(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("prev_event_hash", sa.String(), nullable=False),
        sa.Column("event_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"]),
        sa.UniqueConstraint("game_id", "sequence", name="uq_game_event_sequence"),
    )

    op.create_table(
        "llm_calls",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("game_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=True),
        sa.Column("agent_build_id", sa.Uuid(), nullable=True),
        sa.Column("public_player_id", sa.String(), nullable=False),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("request_json", sa.JSON(), nullable=False),
        sa.Column("request_prompt_hash", sa.String(), nullable=False),
        sa.Column("raw_response", sa.String(), nullable=True),
        sa.Column("parsed_response", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(asdecimal=False), nullable=True),
        sa.Column("provider_response_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["game_id"], ["games.id"]),
        sa.ForeignKeyConstraint(["event_id"], ["game_events.id"]),
        sa.ForeignKeyConstraint(["agent_build_id"], ["agent_builds.id"]),
    )


def downgrade() -> None:
    op.drop_table("llm_calls")
    op.drop_table("game_events")
