"""CRUD helpers for :class:`padrino.db.models.League`."""

from __future__ import annotations

import uuid

from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.enums import LeagueKind
from padrino.db.models import League
from padrino.db.repositories import rating_contexts

HUMANS_INCLUDED_LEAGUE_NAME = "Humans-Included League"


async def create(
    session: AsyncSession,
    *,
    name: str,
    ruleset_id: str,
    ranked: bool,
    kind: LeagueKind = LeagueKind.SCIENTIFIC,
) -> League:
    obj = League(name=name, ruleset_id=ruleset_id, ranked=ranked, kind=kind.value)
    session.add(obj)
    await session.flush()
    if kind is LeagueKind.SCIENTIFIC:
        await rating_contexts.ensure_declared_context(session, ruleset_id=ruleset_id)
    return obj


async def get_or_create_humans_included(
    session: AsyncSession,
    *,
    ruleset_id: str,
    ranked: bool = False,
) -> League:
    """Return the Humans-Included league for one ruleset and ranked mode.

    The humans-included league is discriminated by ``kind=HUMANS_INCLUDED`` so
    scientific vs human leagues are queryable. Human games reference it; it is
    the home of the segregated ``human_rating`` schema and NEVER writes a
    scientific rating row.
    """

    def _select_existing() -> Select[tuple[League]]:
        return (
            select(League)
            .where(
                League.kind == LeagueKind.HUMANS_INCLUDED.value,
                League.ruleset_id == ruleset_id,
                League.ranked == ranked,
            )
            .limit(1)
        )

    found = (await session.execute(_select_existing())).scalar_one_or_none()
    if found is not None:
        return found

    obj = League(
        name=HUMANS_INCLUDED_LEAGUE_NAME,
        ruleset_id=ruleset_id,
        ranked=ranked,
        kind=LeagueKind.HUMANS_INCLUDED.value,
    )
    session.add(obj)
    try:
        # A savepoint isolates the conflicting insert: a concurrent creator that
        # won the unique (kind, ruleset_id) race trips IntegrityError here, and
        # we roll back to the savepoint and re-read its row rather than poisoning
        # the caller's transaction.
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:
        existing = (await session.execute(_select_existing())).scalar_one_or_none()
        if existing is None:  # pragma: no cover - the unique winner must exist
            raise
        return existing
    return obj


async def get(session: AsyncSession, league_id: uuid.UUID) -> League | None:
    return await session.get(League, league_id)


async def list_(
    session: AsyncSession,
    *,
    ranked: bool | None = None,
    ruleset_id: str | None = None,
) -> list[League]:
    stmt = select(League)
    if ranked is not None:
        stmt = stmt.where(League.ranked == ranked)
    if ruleset_id is not None:
        stmt = stmt.where(League.ruleset_id == ruleset_id)
    stmt = stmt.order_by(League.created_at, League.id)
    result = await session.execute(stmt)
    return list(result.scalars())
