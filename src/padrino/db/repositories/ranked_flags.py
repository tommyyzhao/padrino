"""Repository helpers for ranked anti-cheat advisory flags."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.anticheat.signals import RankedFlag as ScoredRankedFlag
from padrino.db.models import RankedFlag


def evidence_hash(evidence: Mapping[str, object]) -> str:
    """Return a deterministic digest for a flag evidence payload."""
    payload = json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def record_ranked_flag(
    session: AsyncSession,
    *,
    flag: ScoredRankedFlag,
    created_at: datetime,
) -> RankedFlag | None:
    """Append one advisory flag, returning None on idempotent conflict."""
    values = {
        "id": uuid.uuid4(),
        "kind": flag.kind,
        "principal_id": flag.principal_id,
        "severity": flag.severity,
        "evidence": flag.evidence,
        "evidence_hash": evidence_hash(flag.evidence),
        "created_at": created_at,
    }
    index_elements = ["kind", "principal_id", "evidence_hash"]
    bind = session.get_bind()
    dialect_name = bind.dialect.name
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = (
            pg_insert(RankedFlag)
            .values(**values)
            .on_conflict_do_nothing(index_elements=index_elements)
            .returning(RankedFlag.id)
        )
    elif dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = (
            sqlite_insert(RankedFlag)
            .values(**values)
            .on_conflict_do_nothing(index_elements=index_elements)
            .returning(RankedFlag.id)
        )
    else:
        raise RuntimeError(f"unsupported dialect for ranked-flag insert: {dialect_name!r}")

    inserted_id = (await session.execute(stmt)).scalar_one_or_none()
    await session.flush()
    if inserted_id is None:
        return None
    inserted = await session.get(RankedFlag, inserted_id)
    if inserted is None:  # pragma: no cover - inserted in the same transaction.
        msg = f"Ranked flag {inserted_id} disappeared after insert"
        raise RuntimeError(msg)
    return inserted


async def list_ranked_flags(session: AsyncSession) -> list[RankedFlag]:
    """Return advisory flags in deterministic order."""
    stmt = select(RankedFlag).order_by(
        RankedFlag.created_at,
        RankedFlag.kind,
        RankedFlag.principal_id,
        RankedFlag.evidence_hash,
        RankedFlag.id,
    )
    return list((await session.execute(stmt)).scalars())


__all__ = ["evidence_hash", "list_ranked_flags", "record_ranked_flag"]
