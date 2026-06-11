"""US-095: Global spend governor — hard $200 ceiling tests."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.models import Game, LlmCall
from padrino.economics.spend_governor import can_start_game, cumulative_spend_usd
from padrino.settings import Settings


def _settings(cap: float = 200.0) -> Settings:
    return Settings(padrino_global_spend_cap_usd=cap)


async def _seed_game(session: AsyncSession) -> uuid.UUID:
    game_id = uuid.uuid4()
    game = Game(
        id=game_id,
        ruleset_id="mini7_v1",
        game_seed="seed-economics",
        status="COMPLETED",
    )
    session.add(game)
    await session.flush()
    return game_id


async def _add_call(
    session: AsyncSession,
    game_id: uuid.UUID,
    cost: float | None,
) -> None:
    session.add(
        LlmCall(
            game_id=game_id,
            public_player_id="P01",
            phase="DAY_DISCUSSION",
            request_json={},
            request_prompt_hash="hash",
            status="ok",
            cost_usd=cost,
        )
    )
    await session.flush()


@pytest.mark.asyncio
async def test_zero_spend_can_start(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session, session.begin():
        result = await can_start_game(session, _settings(cap=200.0))
    assert result is True


@pytest.mark.asyncio
async def test_below_cap_can_start(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session, session.begin():
        gid = await _seed_game(session)
        await _add_call(session, gid, 99.99)

    async with session_factory() as session:
        result = await can_start_game(session, _settings(cap=200.0))
    assert result is True


@pytest.mark.asyncio
async def test_exactly_at_cap_denied(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Admission flips to denied exactly at the cap (spent >= cap)."""
    async with session_factory() as session, session.begin():
        gid = await _seed_game(session)
        await _add_call(session, gid, 200.0)

    async with session_factory() as session:
        result = await can_start_game(session, _settings(cap=200.0))
    assert result is False


@pytest.mark.asyncio
async def test_above_cap_denied(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session, session.begin():
        gid = await _seed_game(session)
        await _add_call(session, gid, 250.0)

    async with session_factory() as session:
        result = await can_start_game(session, _settings(cap=200.0))
    assert result is False


@pytest.mark.asyncio
async def test_multi_game_cumulative(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Spend is summed across multiple games."""
    async with session_factory() as session, session.begin():
        gid1 = await _seed_game(session)
        gid2 = await _seed_game(session)
        await _add_call(session, gid1, 100.0)
        await _add_call(session, gid2, 100.0)

    async with session_factory() as session:
        result = await can_start_game(session, _settings(cap=200.0))
    assert result is False


@pytest.mark.asyncio
async def test_null_cost_rows_excluded(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """LlmCall rows with cost_usd=None are treated as zero by coalesce."""
    async with session_factory() as session, session.begin():
        gid = await _seed_game(session)
        await _add_call(session, gid, None)
        await _add_call(session, gid, None)

    async with session_factory() as session:
        result = await can_start_game(session, _settings(cap=200.0))
    assert result is True


@pytest.mark.asyncio
async def test_cap_read_from_settings(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Cap threshold is read from settings, not hard-coded."""
    async with session_factory() as session, session.begin():
        gid = await _seed_game(session)
        await _add_call(session, gid, 50.0)

    async with session_factory() as session:
        assert await can_start_game(session, _settings(cap=100.0)) is True
        assert await can_start_game(session, _settings(cap=50.0)) is False


@pytest.mark.asyncio
async def test_default_cap_is_200(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Default cap in settings is $200."""
    s = Settings()
    assert s.padrino_global_spend_cap_usd == 200.0


@pytest.mark.asyncio
async def test_cumulative_spend_zero_when_empty(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        total = await cumulative_spend_usd(session)
    assert total == 0.0


@pytest.mark.asyncio
async def test_cumulative_spend_sums_all_calls(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        gid = await _seed_game(session)
        await _add_call(session, gid, 10.0)
        await _add_call(session, gid, 5.5)
        await _add_call(session, gid, None)

    async with session_factory() as session:
        total = await cumulative_spend_usd(session)
    assert abs(total - 15.5) < 1e-9
