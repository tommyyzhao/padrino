"""Add hot-path secondary indexes for game events, LLM calls, and retention.

Revision ID: 0050
Revises: 0049
Create Date: 2026-06-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GAMES_RETENTION_INDEX = "ix_games_completed_at_is_broadcastable"
_GAME_EVENTS_GAME_ID_INDEX = "ix_game_events_game_id"
_LLM_CALLS_GAME_AGENT_EVENT_INDEX = "ix_llm_calls_game_id_agent_build_id_event_id"
_LLM_CALLS_RAW_RESPONSE_INDEX = "ix_llm_calls_game_id_raw_response_present"

_COMPLETED_GAME_WHERE = sa.text("completed_at IS NOT NULL")
_RAW_RESPONSE_WHERE = sa.text("raw_response IS NOT NULL")


def upgrade() -> None:
    op.create_index(
        _GAMES_RETENTION_INDEX,
        "games",
        ["completed_at", "is_broadcastable"],
        sqlite_where=_COMPLETED_GAME_WHERE,
        postgresql_where=_COMPLETED_GAME_WHERE,
    )
    op.create_index(_GAME_EVENTS_GAME_ID_INDEX, "game_events", ["game_id"])
    op.create_index(
        _LLM_CALLS_GAME_AGENT_EVENT_INDEX,
        "llm_calls",
        ["game_id", "agent_build_id", "event_id"],
    )
    op.create_index(
        _LLM_CALLS_RAW_RESPONSE_INDEX,
        "llm_calls",
        ["game_id"],
        sqlite_where=_RAW_RESPONSE_WHERE,
        postgresql_where=_RAW_RESPONSE_WHERE,
    )


def downgrade() -> None:
    op.drop_index(_LLM_CALLS_RAW_RESPONSE_INDEX, table_name="llm_calls")
    op.drop_index(_LLM_CALLS_GAME_AGENT_EVENT_INDEX, table_name="llm_calls")
    op.drop_index(_GAME_EVENTS_GAME_ID_INDEX, table_name="game_events")
    op.drop_index(_GAMES_RETENTION_INDEX, table_name="games")
