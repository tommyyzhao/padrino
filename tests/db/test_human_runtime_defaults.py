"""Database defaults for durable human runtime state."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.rulesets import mini7_v1
from padrino.db.models import Game, HumanGameRuntime


async def test_human_game_runtime_buffer_snapshot_has_database_default(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A raw DB insert can omit ``buffer_snapshot`` and still gets ``{}``."""
    updated_at = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)
    async with session_factory() as session, session.begin():
        game = Game(
            ruleset_id=mini7_v1.RULESET_ID,
            game_seed="runtime-default-buffer",
            status="RUNNING",
        )
        session.add(game)
        await session.flush()
        await session.execute(
            text(
                "INSERT INTO human_game_runtime (game_id, phase, updated_at) "
                "VALUES (:game_id, :phase, :updated_at)"
            ),
            {
                "game_id": game.id.hex,
                "phase": "DAY_1_DISCUSSION",
                "updated_at": updated_at,
            },
        )
        game_id = game.id

    async with session_factory() as session:
        runtime = await session.get(HumanGameRuntime, game_id)

    assert runtime is not None
    assert runtime.buffer_snapshot == {}
