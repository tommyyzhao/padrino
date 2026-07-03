"""Repository helpers for ranked game exclusions and rollback audit rows."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import RankedGameExclusion, RankedRatingRollbackAudit


def exclusion_set_hash(excluded_game_ids: list[str]) -> str:
    """Return the stable idempotency hash for a sorted exclusion set."""
    payload = json.dumps(sorted(excluded_game_ids), separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def record_exclusion(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    human_player_id: str,
    game_id: uuid.UUID,
    reason: str,
    created_by: str,
    created_at: datetime,
) -> RankedGameExclusion | None:
    """Append one ranked-game exclusion, returning None on idempotent conflict."""
    values = {
        "id": uuid.uuid4(),
        "principal_id": principal_id,
        "human_player_id": human_player_id,
        "game_id": game_id,
        "reason": reason,
        "created_by": created_by,
        "created_at": created_at,
    }
    bind = session.get_bind()
    dialect_name = bind.dialect.name
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = (
            pg_insert(RankedGameExclusion)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["principal_id", "game_id"],
            )
            .returning(RankedGameExclusion.id)
        )
    elif dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = (
            sqlite_insert(RankedGameExclusion)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=["principal_id", "game_id"],
            )
            .returning(RankedGameExclusion.id)
        )
    else:
        raise RuntimeError(
            f"unsupported dialect for ranked-game exclusion insert: {dialect_name!r}"
        )

    inserted_id = (await session.execute(stmt)).scalar_one_or_none()
    await session.flush()
    if inserted_id is None:
        return None
    inserted = await session.get(RankedGameExclusion, inserted_id)
    if inserted is None:  # pragma: no cover - inserted in the same transaction.
        msg = f"Ranked game exclusion {inserted_id} disappeared after insert"
        raise RuntimeError(msg)
    return inserted


async def record_rollback_audit(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    human_player_id: str,
    reason: str,
    excluded_game_ids: list[str],
    created_by: str,
    created_at: datetime,
) -> RankedRatingRollbackAudit | None:
    """Append one rollback audit row, returning None for the same exclusion set."""
    sorted_ids = sorted(excluded_game_ids)
    values = {
        "id": uuid.uuid4(),
        "principal_id": principal_id,
        "human_player_id": human_player_id,
        "reason": reason,
        "excluded_game_ids": sorted_ids,
        "exclusion_hash": exclusion_set_hash(sorted_ids),
        "created_by": created_by,
        "created_at": created_at,
    }
    bind = session.get_bind()
    dialect_name = bind.dialect.name
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = (
            pg_insert(RankedRatingRollbackAudit)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["principal_id", "exclusion_hash"])
            .returning(RankedRatingRollbackAudit.id)
        )
    elif dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = (
            sqlite_insert(RankedRatingRollbackAudit)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["principal_id", "exclusion_hash"])
            .returning(RankedRatingRollbackAudit.id)
        )
    else:
        raise RuntimeError(f"unsupported dialect for rollback audit insert: {dialect_name!r}")

    inserted_id = (await session.execute(stmt)).scalar_one_or_none()
    await session.flush()
    if inserted_id is None:
        return None
    inserted = await session.get(RankedRatingRollbackAudit, inserted_id)
    if inserted is None:  # pragma: no cover - inserted in the same transaction.
        msg = f"Ranked rating rollback audit {inserted_id} disappeared after insert"
        raise RuntimeError(msg)
    return inserted


__all__ = [
    "exclusion_set_hash",
    "record_exclusion",
    "record_rollback_audit",
]
