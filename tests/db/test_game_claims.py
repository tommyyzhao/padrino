"""US-252: game-grain lease claims and stale-game reset helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.game_status import GAME_STATUS_CREATED, GAME_STATUS_RUNNING
from padrino.db.repositories import games

_NOW = datetime(2026, 6, 24, 12, tzinfo=UTC)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def test_claim_oldest_pending_game_sqlite_stamps_lease_and_attempt(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        game = await games.create(
            session,
            ruleset_id="mini7_v1",
            game_seed="sqlite-claim",
            status=GAME_STATUS_CREATED,
        )
        game_id = game.id

    async with session_factory() as session, session.begin():
        claimed = await games.claim_oldest_pending_game(
            session,
            now=_NOW,
            lease_ttl=timedelta(seconds=30),
            worker_id="worker-a",
        )

    assert claimed is not None
    assert claimed.id == game_id
    assert claimed.status == GAME_STATUS_RUNNING
    assert claimed.leased_by == "worker-a"
    assert claimed.attempt_count == 1
    assert claimed.lease_expires_at is not None
    assert _aware(claimed.lease_expires_at) == _NOW + timedelta(seconds=30)


async def test_reset_stale_games_clears_expired_lease_and_makes_it_reclaimable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session, session.begin():
        expired = await games.create(
            session,
            ruleset_id="mini7_v1",
            game_seed="expired-lease",
            status=GAME_STATUS_RUNNING,
        )
        expired.leased_by = "dead-worker"
        expired.lease_expires_at = _NOW - timedelta(seconds=1)
        fresh = await games.create(
            session,
            ruleset_id="mini7_v1",
            game_seed="fresh-lease",
            status=GAME_STATUS_RUNNING,
        )
        fresh.leased_by = "live-worker"
        fresh.lease_expires_at = _NOW + timedelta(seconds=30)
        expired_id = expired.id
        fresh_id = fresh.id

    async with session_factory() as session, session.begin():
        reset_ids = await games.reset_stale_games(session, now=_NOW)

    assert reset_ids == [expired_id]

    async with session_factory() as session:
        expired_after = await games.get(session, expired_id)
        fresh_after = await games.get(session, fresh_id)

    assert expired_after is not None
    assert expired_after.status == GAME_STATUS_RUNNING
    assert expired_after.leased_by is None
    assert expired_after.lease_expires_at is None
    assert fresh_after is not None
    assert fresh_after.leased_by == "live-worker"
    assert fresh_after.lease_expires_at is not None
    assert _aware(fresh_after.lease_expires_at) == _NOW + timedelta(seconds=30)

    async with session_factory() as session, session.begin():
        reclaimed = await games.claim_oldest_pending_game(
            session,
            now=_NOW,
            lease_ttl=timedelta(seconds=10),
            worker_id="worker-b",
        )

    assert reclaimed is not None
    assert reclaimed.id == expired_id
    assert reclaimed.leased_by == "worker-b"
    assert reclaimed.lease_expires_at is not None
    assert _aware(reclaimed.lease_expires_at) == _NOW + timedelta(seconds=10)


async def test_reset_stale_games_uses_injected_clock(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    lease_expires_at = _NOW + timedelta(seconds=10)
    async with session_factory() as session, session.begin():
        game = await games.create(
            session,
            ruleset_id="mini7_v1",
            game_seed="clock-injected",
            status=GAME_STATUS_RUNNING,
        )
        game.leased_by = "clock-worker"
        game.lease_expires_at = lease_expires_at
        game_id = game.id

    async with session_factory() as session, session.begin():
        early_reset_ids = await games.reset_stale_games(
            session,
            now=_NOW + timedelta(seconds=9),
        )

    assert early_reset_ids == []

    async with session_factory() as session, session.begin():
        late_reset_ids = await games.reset_stale_games(
            session,
            now=_NOW + timedelta(seconds=11),
        )

    assert late_reset_ids == [game_id]
