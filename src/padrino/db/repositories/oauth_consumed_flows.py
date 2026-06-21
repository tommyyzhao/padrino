"""Single-use ledger for OAuth authorization flows (US-202).

OAuth ``state``/``nonce`` are stateless signed tokens with no inherent single-use
guarantee, so a replayed ``(state cookie, code)`` pair could mint a second session
within the flow-cookie TTL if the upstream provider had not yet invalidated the
authorization code. :func:`try_consume_flow` records the per-flow unique token (the
``flow`` claim embedded in the signed state) with an atomic
``INSERT ... ON CONFLICT DO NOTHING`` and reports whether THIS call claimed it, so
the callback can reject a replayed flow fail-closed BEFORE exchanging the code.

This module never reads a clock; the consumed-at timestamp is injected by the
caller (repository-purity guard). Rows older than the flow TTL are inert and may
be pruned by :func:`prune_expired`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import OAuthConsumedFlow


async def try_consume_flow(
    session: AsyncSession,
    *,
    flow: str,
    consumed_at: datetime,
) -> bool:
    """Atomically claim ``flow`` as consumed; return True only if THIS call won.

    Uses a dialect-aware ``INSERT ... ON CONFLICT DO NOTHING`` so two concurrent
    callbacks replaying the same flow token cannot both succeed: the first insert
    wins (returns True), every later attempt is a no-op (returns False). The
    caller MUST treat a False result as a replayed/duplicate flow and reject the
    callback fail-closed.
    """
    bind = session.get_bind()
    dialect_name = bind.dialect.name
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = (
            pg_insert(OAuthConsumedFlow)
            .values(flow=flow, consumed_at=consumed_at)
            .on_conflict_do_nothing(index_elements=["flow"])
            .returning(OAuthConsumedFlow.flow)
        )
    elif dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = (
            sqlite_insert(OAuthConsumedFlow)
            .values(flow=flow, consumed_at=consumed_at)
            .on_conflict_do_nothing(index_elements=["flow"])
            .returning(OAuthConsumedFlow.flow)
        )
    else:
        raise RuntimeError(f"unsupported dialect for consumed-flow upsert: {dialect_name!r}")

    result = await session.execute(stmt)
    await session.flush()
    return result.scalar_one_or_none() is not None


async def prune_expired(
    session: AsyncSession,
    *,
    older_than: datetime,
) -> int:
    """Delete consumed-flow rows recorded before ``older_than``; return the count.

    Rows past the flow TTL can never block a NEW flow (each flow token is fresh),
    so they are purely garbage. Pruning keeps the ledger small; a stale row that
    survives a missed sweep is harmless.
    """
    result = await session.execute(
        delete(OAuthConsumedFlow).where(OAuthConsumedFlow.consumed_at < older_than)
    )
    await session.flush()
    assert isinstance(result, CursorResult)
    return result.rowcount or 0


__all__ = ["prune_expired", "try_consume_flow"]
