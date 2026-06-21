"""CRUD helpers for human seat presence heartbeats (US-162).

Presence is impure transport metadata for human games: routes record a seat's
latest heartbeat, and the human worker lane reads those rows to decide whether a
seat has exceeded its reconnect grace window. This repository never reads a
clock; all timestamps are injected by the caller.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import HumanSeatPresence


async def get(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    public_player_id: str,
    for_update: bool = False,
) -> HumanSeatPresence | None:
    """Return one seat presence row, or ``None`` if no heartbeat was recorded.

    Pass ``for_update=True`` to take a row lock (``SELECT ... FOR UPDATE``) so a
    racing heartbeat is serialized against the caller's transaction. On SQLite
    (no row-level locks) ``with_for_update`` is a portable no-op; the takeover
    lane's correctness on SQLite already rests on its single-writer model.
    """
    stmt = select(HumanSeatPresence).where(
        HumanSeatPresence.game_id == game_id,
        HumanSeatPresence.public_player_id == public_player_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_for_game(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
) -> list[HumanSeatPresence]:
    """Return every presence row for ``game_id`` ordered by seat id."""
    stmt = (
        select(HumanSeatPresence)
        .where(HumanSeatPresence.game_id == game_id)
        .order_by(HumanSeatPresence.public_player_id)
    )
    return list((await session.execute(stmt)).scalars())


async def record_heartbeat(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    public_player_id: str,
    seen_at: datetime,
) -> HumanSeatPresence:
    """Atomically upsert a connected heartbeat for one human seat.

    Uses ``INSERT ... ON CONFLICT (game_id, public_player_id) DO UPDATE`` so two
    concurrent heartbeats for the same seat never both see "no row", both INSERT,
    and the second violate ``uq_human_seat_presence`` (which previously surfaced
    as a 500 on the presence/observation/action path).
    """
    return await _upsert_presence(
        session,
        game_id=game_id,
        public_player_id=public_player_id,
        connected=True,
        last_seen_at=seen_at,
        disconnected_at=None,
        updated_at=seen_at,
    )


async def mark_disconnected(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    public_player_id: str,
    disconnected_at: datetime,
) -> HumanSeatPresence:
    """Atomically upsert an explicit disconnect for one human seat.

    Shares the race-safe ``ON CONFLICT DO UPDATE`` path with
    :func:`record_heartbeat`; concurrent calls cannot raise ``IntegrityError``.
    A disconnect deliberately leaves ``last_seen_at`` untouched (the last live
    heartbeat) so the worker lane can measure the grace window from it.
    """
    return await _upsert_presence(
        session,
        game_id=game_id,
        public_player_id=public_player_id,
        connected=False,
        last_seen_at=_KEEP,
        disconnected_at=disconnected_at,
        updated_at=disconnected_at,
    )


# Sentinel for "do not change this column on conflict" (None is a real value).
_KEEP = object()


async def _upsert_presence(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    public_player_id: str,
    connected: bool,
    last_seen_at: datetime | None | object,
    disconnected_at: datetime | None,
    updated_at: datetime,
) -> HumanSeatPresence:
    """Dialect-aware MONOTONIC ``INSERT ... ON CONFLICT DO UPDATE`` (US-200).

    The ORM-level ``id`` / ``updated_at`` defaults do not fire on a Core insert,
    so every column is supplied explicitly. Portable across SQLite + Postgres.

    The update is guarded by ``WHERE excluded.updated_at >= existing.updated_at``
    so an OLDER write (e.g. an out-of-order heartbeat, or a delayed disconnect
    racing a newer reconnect) can never regress the row: the conflict becomes a
    no-op and the freshly committed row is preserved. ``last_seen_at`` is set via
    ``GREATEST``/``MAX`` as belt-and-braces against regressing the last live
    heartbeat even under an equal-``updated_at`` edge. When the guard rejects the
    write the caller still re-reads and returns the current row (no assertion
    that THIS call wrote it).
    """
    insert_values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "game_id": game_id,
        "public_player_id": public_player_id,
        "connected": connected,
        "last_seen_at": None if last_seen_at is _KEEP else last_seen_at,
        "disconnected_at": disconnected_at,
        "updated_at": updated_at,
    }

    bind = session.get_bind()
    dialect_name = bind.dialect.name
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        from sqlalchemy.sql import func as sql_func

        pg_ins = pg_insert(HumanSeatPresence)
        pg_excluded = pg_ins.excluded
        pg_update: dict[str, Any] = {
            "connected": pg_excluded.connected,
            "disconnected_at": pg_excluded.disconnected_at,
            "updated_at": pg_excluded.updated_at,
        }
        if last_seen_at is not _KEEP:
            pg_update["last_seen_at"] = sql_func.greatest(
                sql_func.coalesce(HumanSeatPresence.last_seen_at, pg_excluded.last_seen_at),
                pg_excluded.last_seen_at,
            )
        stmt = (
            pg_ins.values(**insert_values)
            .on_conflict_do_update(
                index_elements=["game_id", "public_player_id"],
                set_=pg_update,
                where=pg_excluded.updated_at >= HumanSeatPresence.updated_at,
            )
            .returning(HumanSeatPresence.id)
        )
    elif dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert
        from sqlalchemy.sql import func as sql_func

        sqlite_ins = sqlite_insert(HumanSeatPresence)
        sqlite_excluded = sqlite_ins.excluded
        sqlite_update: dict[str, Any] = {
            "connected": sqlite_excluded.connected,
            "disconnected_at": sqlite_excluded.disconnected_at,
            "updated_at": sqlite_excluded.updated_at,
        }
        if last_seen_at is not _KEEP:
            sqlite_update["last_seen_at"] = sql_func.max(
                sql_func.coalesce(HumanSeatPresence.last_seen_at, sqlite_excluded.last_seen_at),
                sqlite_excluded.last_seen_at,
            )
        stmt = (
            sqlite_ins.values(**insert_values)
            .on_conflict_do_update(
                index_elements=["game_id", "public_player_id"],
                set_=sqlite_update,
                where=sqlite_excluded.updated_at >= HumanSeatPresence.updated_at,
            )
            .returning(HumanSeatPresence.id)
        )
    else:
        raise RuntimeError(f"unsupported dialect for presence upsert: {dialect_name!r}")

    await session.execute(stmt)
    await session.flush()
    # The Core upsert bypasses the ORM, so any instance already in the identity
    # map holds stale column values. Expire it before re-reading so the caller
    # sees the values just written.
    existing = await get(session, game_id=game_id, public_player_id=public_player_id)
    if existing is not None:
        session.expire(existing)
    row = await get(session, game_id=game_id, public_player_id=public_player_id)
    # A row always exists post-upsert: either THIS call inserted it, or a
    # conflicting row was present (whether or not the monotonic WHERE guard let
    # this older write update it). The no-op (older-write-loses) branch still
    # returns the current, fresher row rather than asserting this call wrote.
    assert row is not None
    return row


__all__ = [
    "get",
    "list_for_game",
    "mark_disconnected",
    "record_heartbeat",
]
