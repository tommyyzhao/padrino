"""US-192: race-safe human seat presence heartbeats (atomic upsert).

``record_heartbeat`` / ``mark_disconnected`` used a read-then-insert that raced:
two concurrent heartbeats for the same ``(game_id, public_player_id)`` both saw
no row, both INSERTed, and the second violated ``uq_human_seat_presence`` ->
``IntegrityError`` surfacing as a 500 on the presence/observation/action path.

These tests assert the dialect-aware ``INSERT ... ON CONFLICT DO UPDATE`` never
raises under concurrency and leaves a single coherent presence row.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from padrino.db.base import Base, create_engine, create_session_factory
from padrino.db.models import Game
from padrino.db.repositories import human_seat_presence as presence_repo

_BASE = datetime(2026, 6, 20, 12, 0, 0, tzinfo=UTC)


def _naive(value: datetime | None) -> datetime | None:
    """Drop tzinfo for comparison (SQLite round-trips datetimes as naive)."""
    return value.replace(tzinfo=None) if value is not None else None


async def _seed_game(session_factory) -> uuid.UUID:  # type: ignore[no-untyped-def]
    async with session_factory() as session, session.begin():
        game = Game(
            gauntlet_id=None,
            ruleset_id="mini7_v1",
            game_seed="presence-race",
            status="RUNNING",
        )
        session.add(game)
        await session.flush()
        return game.id


async def test_concurrent_heartbeats_no_integrity_error(tmp_path: Path) -> None:
    """Simultaneous heartbeats for one seat never raise and leave one row."""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'presence-race.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    try:
        game_id = await _seed_game(session_factory)
        ready = asyncio.Event()

        async def beat(index: int) -> None:
            await ready.wait()
            async with session_factory() as session, session.begin():
                await presence_repo.record_heartbeat(
                    session,
                    game_id=game_id,
                    public_player_id="P01",
                    seen_at=_BASE + timedelta(seconds=index),
                )

        tasks = [asyncio.create_task(beat(i)) for i in range(8)]
        ready.set()
        # No IntegrityError must escape any concurrent writer.
        await asyncio.gather(*tasks)

        async with session_factory() as session:
            rows = await presence_repo.list_for_game(session, game_id=game_id)
    finally:
        await engine.dispose()

    assert len(rows) == 1
    row = rows[0]
    assert row.public_player_id == "P01"
    assert row.connected is True
    assert row.disconnected_at is None
    assert row.last_seen_at is not None


async def test_concurrent_heartbeat_and_disconnect_single_row(tmp_path: Path) -> None:
    """Mixing heartbeat + disconnect concurrently still yields one coherent row."""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'presence-mix.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    try:
        game_id = await _seed_game(session_factory)
        ready = asyncio.Event()

        async def beat() -> None:
            await ready.wait()
            async with session_factory() as session, session.begin():
                await presence_repo.record_heartbeat(
                    session,
                    game_id=game_id,
                    public_player_id="P02",
                    seen_at=_BASE,
                )

        async def disconnect() -> None:
            await ready.wait()
            async with session_factory() as session, session.begin():
                await presence_repo.mark_disconnected(
                    session,
                    game_id=game_id,
                    public_player_id="P02",
                    disconnected_at=_BASE + timedelta(seconds=5),
                )

        tasks = [asyncio.create_task(beat()) for _ in range(4)]
        tasks += [asyncio.create_task(disconnect()) for _ in range(4)]
        ready.set()
        await asyncio.gather(*tasks)

        async with session_factory() as session:
            rows = await presence_repo.list_for_game(session, game_id=game_id)
    finally:
        await engine.dispose()

    assert len(rows) == 1


async def test_heartbeat_then_disconnect_preserves_last_seen(tmp_path: Path) -> None:
    """A disconnect keeps the last live heartbeat for grace-window measurement."""
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'presence-keep.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = create_session_factory(engine)
    try:
        game_id = await _seed_game(session_factory)
        async with session_factory() as session, session.begin():
            beat = await presence_repo.record_heartbeat(
                session,
                game_id=game_id,
                public_player_id="P03",
                seen_at=_BASE,
            )
            assert beat.connected is True
            assert _naive(beat.last_seen_at) == _naive(_BASE)

        async with session_factory() as session, session.begin():
            row = await presence_repo.mark_disconnected(
                session,
                game_id=game_id,
                public_player_id="P03",
                disconnected_at=_BASE + timedelta(minutes=2),
            )
            assert row.connected is False
            assert _naive(row.disconnected_at) == _naive(_BASE + timedelta(minutes=2))
            # The last live heartbeat is preserved (not clobbered to None).
            assert _naive(row.last_seen_at) == _naive(_BASE)

        # A subsequent reconnect re-marks connected and clears the disconnect.
        async with session_factory() as session, session.begin():
            row = await presence_repo.record_heartbeat(
                session,
                game_id=game_id,
                public_player_id="P03",
                seen_at=_BASE + timedelta(minutes=3),
            )
            assert row.connected is True
            assert row.disconnected_at is None
            assert _naive(row.last_seen_at) == _naive(_BASE + timedelta(minutes=3))
    finally:
        await engine.dispose()
