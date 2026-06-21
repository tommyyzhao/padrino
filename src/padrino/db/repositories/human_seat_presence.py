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
) -> HumanSeatPresence | None:
    """Return one seat presence row, or ``None`` if no heartbeat was recorded."""
    stmt = select(HumanSeatPresence).where(
        HumanSeatPresence.game_id == game_id,
        HumanSeatPresence.public_player_id == public_player_id,
    )
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
    """Dialect-aware ``INSERT ... ON CONFLICT DO UPDATE`` for one presence row.

    The ORM-level ``id`` / ``updated_at`` defaults do not fire on a Core insert,
    so every column is supplied explicitly. Portable across SQLite + Postgres.
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
    update_values: dict[str, Any] = {
        "connected": connected,
        "disconnected_at": disconnected_at,
        "updated_at": updated_at,
    }
    if last_seen_at is not _KEEP:
        update_values["last_seen_at"] = last_seen_at

    bind = session.get_bind()
    dialect_name = bind.dialect.name
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = (
            pg_insert(HumanSeatPresence)
            .values(**insert_values)
            .on_conflict_do_update(
                index_elements=["game_id", "public_player_id"],
                set_=update_values,
            )
            .returning(HumanSeatPresence.id)
        )
    elif dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = (
            sqlite_insert(HumanSeatPresence)
            .values(**insert_values)
            .on_conflict_do_update(
                index_elements=["game_id", "public_player_id"],
                set_=update_values,
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
    assert row is not None  # the upsert guarantees a row exists
    return row


__all__ = [
    "get",
    "list_for_game",
    "mark_disconnected",
    "record_heartbeat",
]
