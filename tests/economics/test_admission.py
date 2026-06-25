"""US-096: Admission / queue policy — daily and concurrency caps."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.game_status import (
    GAME_STATUS_COMPLETED,
    GAME_STATUS_CREATED,
    GAME_STATUS_FAILED,
    GAME_STATUS_RUNNING,
)
from padrino.db.models import Game, LlmCall
from padrino.economics.admission import AdmitDecision, admit
from padrino.settings import Settings

_NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)
_TODAY_START = _NOW.replace(hour=0, minute=0, second=0, microsecond=0)
_YESTERDAY_START = _TODAY_START - timedelta(days=1)


def _settings(
    *,
    spend_cap: float = 200.0,
    max_per_day: int = 20,
    max_concurrent: int = 3,
) -> Settings:
    return Settings(
        padrino_global_spend_cap_usd=spend_cap,
        padrino_max_games_per_day=max_per_day,
        padrino_max_concurrent_games=max_concurrent,
    )


async def _seed_game(
    session: AsyncSession,
    *,
    status: str = GAME_STATUS_COMPLETED,
    created_at: datetime | None = None,
) -> uuid.UUID:
    game = Game(
        id=uuid.uuid4(),
        ruleset_id="mini7_v1",
        game_seed="seed-admission",
        status=status,
    )
    if created_at is not None:
        game.created_at = created_at
    session.add(game)
    await session.flush()
    return game.id


async def _add_spend(session: AsyncSession, game_id: uuid.UUID, cost: float) -> None:
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


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admit_all_clear(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """No spend, no daily games, no concurrent games → admitted."""
    async with session_factory() as session:
        decision = await admit(session, _settings(), now=_NOW)
    assert decision == AdmitDecision(allowed=True, reason="admitted")


# ---------------------------------------------------------------------------
# Spend cap denial
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_denied_spend_cap(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Cumulative spend >= cap → spend_cap_reached."""
    async with session_factory() as session, session.begin():
        gid = await _seed_game(session)
        await _add_spend(session, gid, 200.0)

    async with session_factory() as session:
        decision = await admit(session, _settings(spend_cap=200.0), now=_NOW)
    assert decision == AdmitDecision(allowed=False, reason="spend_cap_reached")


@pytest.mark.asyncio
async def test_denied_spend_cap_partial(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Spend just below cap is still admitted."""
    async with session_factory() as session, session.begin():
        gid = await _seed_game(session)
        await _add_spend(session, gid, 199.99)

    async with session_factory() as session:
        decision = await admit(session, _settings(spend_cap=200.0), now=_NOW)
    assert decision.allowed is True


# ---------------------------------------------------------------------------
# Daily cap denial
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_denied_daily_cap(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """daily_count >= max_games_per_day → daily_cap_reached."""
    async with session_factory() as session, session.begin():
        for _ in range(5):
            await _seed_game(session, created_at=_TODAY_START + timedelta(hours=1))

    async with session_factory() as session:
        decision = await admit(session, _settings(max_per_day=5), now=_NOW)
    assert decision == AdmitDecision(allowed=False, reason="daily_cap_reached")


@pytest.mark.asyncio
async def test_daily_cap_ignores_yesterday(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Games created yesterday do not count toward today's daily cap."""
    async with session_factory() as session, session.begin():
        for _ in range(5):
            await _seed_game(session, created_at=_YESTERDAY_START + timedelta(hours=6))

    async with session_factory() as session:
        decision = await admit(session, _settings(max_per_day=5), now=_NOW)
    assert decision.allowed is True


@pytest.mark.asyncio
async def test_daily_cap_boundary_exactly_at_cap(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Exactly max_games_per_day games today → denied (>= check)."""
    async with session_factory() as session, session.begin():
        for _ in range(3):
            await _seed_game(session, created_at=_TODAY_START + timedelta(hours=2))

    async with session_factory() as session:
        decision = await admit(session, _settings(max_per_day=3), now=_NOW)
    assert decision == AdmitDecision(allowed=False, reason="daily_cap_reached")


# ---------------------------------------------------------------------------
# Concurrency cap denial
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_denied_concurrency_cap(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Active non-terminal game count >= max_concurrent → concurrency_cap_reached."""
    async with session_factory() as session, session.begin():
        for _ in range(3):
            await _seed_game(
                session,
                status=GAME_STATUS_CREATED,
                created_at=_YESTERDAY_START,
            )

    async with session_factory() as session:
        decision = await admit(session, _settings(max_concurrent=3), now=_NOW)
    assert decision == AdmitDecision(allowed=False, reason="concurrency_cap_reached")


@pytest.mark.asyncio
async def test_concurrency_cap_ignores_completed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """COMPLETED games are not counted as active."""
    async with session_factory() as session, session.begin():
        for _ in range(5):
            await _seed_game(session, status=GAME_STATUS_COMPLETED, created_at=_YESTERDAY_START)

    async with session_factory() as session:
        decision = await admit(session, _settings(max_concurrent=3), now=_NOW)
    assert decision.allowed is True


@pytest.mark.asyncio
async def test_concurrency_cap_ignores_failed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """FAILED games are terminal and must not consume concurrency slots."""
    async with session_factory() as session, session.begin():
        for _ in range(5):
            await _seed_game(session, status=GAME_STATUS_FAILED, created_at=_YESTERDAY_START)

    async with session_factory() as session:
        decision = await admit(session, _settings(max_concurrent=3), now=_NOW)
    assert decision == AdmitDecision(allowed=True, reason="admitted")


@pytest.mark.asyncio
async def test_concurrency_cap_counts_only_non_terminal_games(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Terminal games free slots while RUNNING games still consume them."""
    async with session_factory() as session, session.begin():
        await _seed_game(session, status=GAME_STATUS_COMPLETED, created_at=_YESTERDAY_START)
        await _seed_game(session, status=GAME_STATUS_FAILED, created_at=_YESTERDAY_START)

    async with session_factory() as session:
        decision = await admit(session, _settings(max_concurrent=1), now=_NOW)
    assert decision == AdmitDecision(allowed=True, reason="admitted")

    async with session_factory() as session, session.begin():
        await _seed_game(session, status=GAME_STATUS_RUNNING, created_at=_YESTERDAY_START)

    async with session_factory() as session:
        decision = await admit(session, _settings(max_concurrent=1), now=_NOW)
    assert decision == AdmitDecision(allowed=False, reason="concurrency_cap_reached")


@pytest.mark.asyncio
async def test_concurrency_cap_boundary(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Exactly max_concurrent active games → denied."""
    async with session_factory() as session, session.begin():
        for _ in range(2):
            await _seed_game(
                session,
                status=GAME_STATUS_CREATED,
                created_at=_YESTERDAY_START,
            )

    async with session_factory() as session:
        decision = await admit(session, _settings(max_concurrent=2), now=_NOW)
    assert decision == AdmitDecision(allowed=False, reason="concurrency_cap_reached")


# ---------------------------------------------------------------------------
# Priority ordering: spend beats daily beats concurrency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spend_takes_priority_over_daily(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When both spend and daily caps are exceeded, spend_cap_reached is returned."""
    async with session_factory() as session, session.begin():
        gid = await _seed_game(session, created_at=_TODAY_START + timedelta(hours=1))
        await _add_spend(session, gid, 200.0)
        # seed extra daily games
        for _ in range(4):
            await _seed_game(session, created_at=_TODAY_START + timedelta(hours=2))

    async with session_factory() as session:
        decision = await admit(session, _settings(spend_cap=200.0, max_per_day=5), now=_NOW)
    assert decision.reason == "spend_cap_reached"


@pytest.mark.asyncio
async def test_daily_takes_priority_over_concurrency(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When both daily and concurrency caps are exceeded, daily_cap_reached is returned."""
    async with session_factory() as session, session.begin():
        for _ in range(3):
            # same game is both today's game AND active (non-terminal)
            await _seed_game(
                session,
                status=GAME_STATUS_CREATED,
                created_at=_TODAY_START + timedelta(hours=1),
            )

    async with session_factory() as session:
        decision = await admit(session, _settings(max_per_day=3, max_concurrent=3), now=_NOW)
    assert decision.reason == "daily_cap_reached"


# ---------------------------------------------------------------------------
# Default settings smoke test
# ---------------------------------------------------------------------------


def test_default_settings_values() -> None:
    """Default settings carry conservative admission caps."""
    s = Settings()
    assert s.padrino_max_games_per_day == 20
    assert s.padrino_max_concurrent_games == 3
