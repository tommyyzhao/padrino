"""Repository helpers for ranked human anti-cheat telemetry."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import RankedGameSignal


async def record_ranked_game_signal(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    human_principal_id: uuid.UUID,
    source_ip_hash: str,
    winner: str,
    faction: str,
    created_at: datetime,
    session_fingerprint_hash: str | None = None,
    signals: dict[str, Any] | None = None,
) -> RankedGameSignal | None:
    """Append one ranked telemetry row per game, returning None on conflict."""
    values = {
        "id": uuid.uuid4(),
        "game_id": game_id,
        "human_principal_id": human_principal_id,
        "source_ip_hash": source_ip_hash,
        "session_fingerprint_hash": session_fingerprint_hash,
        "winner": winner,
        "faction": faction,
        "signals": signals or {},
        "created_at": created_at,
    }
    bind = session.get_bind()
    dialect_name = bind.dialect.name
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = (
            pg_insert(RankedGameSignal)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["game_id"])
            .returning(RankedGameSignal.id)
        )
    elif dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = (
            sqlite_insert(RankedGameSignal)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["game_id"])
            .returning(RankedGameSignal.id)
        )
    else:
        raise RuntimeError(f"unsupported dialect for ranked-game signal insert: {dialect_name!r}")

    inserted_id = (await session.execute(stmt)).scalar_one_or_none()
    await session.flush()
    if inserted_id is None:
        return None
    inserted = await session.get(RankedGameSignal, inserted_id)
    if inserted is None:  # pragma: no cover - inserted in the same transaction.
        msg = f"Ranked game signal {inserted_id} disappeared after insert"
        raise RuntimeError(msg)
    return inserted
