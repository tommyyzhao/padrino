"""CRUD helpers for human-ranked ``HumanRating`` and ``HumanRatingEvent`` rows."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import HumanRating, HumanRatingEvent, RankedWriteAudit


async def get_or_create_human_rating(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    human_player_id: str,
    scope_type: str,
    scope_value: str,
    initial_mu: float,
    initial_sigma: float,
    initial_conservative_score: float,
) -> HumanRating:
    """Return the existing human rating row for the scope, or insert one."""
    stmt = select(HumanRating).where(
        HumanRating.league_id == league_id,
        HumanRating.human_player_id == human_player_id,
        HumanRating.scope_type == scope_type,
        HumanRating.scope_value == scope_value,
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return existing

    obj = HumanRating(
        league_id=league_id,
        human_player_id=human_player_id,
        scope_type=scope_type,
        scope_value=scope_value,
        mu=initial_mu,
        sigma=initial_sigma,
        conservative_score=initial_conservative_score,
        games=0,
    )
    session.add(obj)
    await session.flush()
    return obj


async def update_human_rating(
    session: AsyncSession,
    rating_id: uuid.UUID,
    *,
    mu: float,
    sigma: float,
    conservative_score: float,
    games: int,
    updated_at: datetime | None = None,
    last_game_at: datetime | None = None,
) -> HumanRating | None:
    """Update the mu/sigma/conservative_score/games on a human rating row."""
    rating = await session.get(HumanRating, rating_id)
    if rating is None:
        return None
    rating.mu = mu
    rating.sigma = sigma
    rating.conservative_score = conservative_score
    rating.games = games
    if updated_at is not None:
        rating.updated_at = updated_at
    if last_game_at is not None:
        rating.last_game_at = last_game_at
    await session.flush()
    return rating


async def get_human_rating_event(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    human_player_id: str,
    scope_type: str,
    scope_value: str,
) -> HumanRatingEvent | None:
    """Return the per-game human-rating event for the unique scope, if present."""
    stmt = select(HumanRatingEvent).where(
        HumanRatingEvent.game_id == game_id,
        HumanRatingEvent.human_player_id == human_player_id,
        HumanRatingEvent.scope_type == scope_type,
        HumanRatingEvent.scope_value == scope_value,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def record_human_rating_event(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    game_id: uuid.UUID,
    human_player_id: str,
    scope_type: str,
    scope_value: str,
    before_mu: float,
    before_sigma: float,
    after_mu: float,
    after_sigma: float,
    public_player_id: str | None = None,
) -> HumanRatingEvent | None:
    """Append a human-rating audit row, returning None on idempotent conflict."""
    values = {
        "id": uuid.uuid4(),
        "league_id": league_id,
        "game_id": game_id,
        "human_player_id": human_player_id,
        "public_player_id": public_player_id,
        "scope_type": scope_type,
        "scope_value": scope_value,
        "before_mu": before_mu,
        "before_sigma": before_sigma,
        "after_mu": after_mu,
        "after_sigma": after_sigma,
    }
    index_elements = ["game_id", "human_player_id", "scope_type", "scope_value"]
    bind = session.get_bind()
    dialect_name = bind.dialect.name
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = (
            pg_insert(HumanRatingEvent)
            .values(**values)
            .on_conflict_do_nothing(index_elements=index_elements)
            .returning(HumanRatingEvent.id)
        )
    elif dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = (
            sqlite_insert(HumanRatingEvent)
            .values(**values)
            .on_conflict_do_nothing(index_elements=index_elements)
            .returning(HumanRatingEvent.id)
        )
    else:
        raise RuntimeError(f"unsupported dialect for human-rating event insert: {dialect_name!r}")

    inserted_id = (await session.execute(stmt)).scalar_one_or_none()
    await session.flush()
    if inserted_id is None:
        return None
    inserted = await session.get(HumanRatingEvent, inserted_id)
    if inserted is None:  # pragma: no cover - inserted in the same transaction.
        msg = f"Human rating event {inserted_id} disappeared after insert"
        raise RuntimeError(msg)
    return inserted


async def record_ranked_write_audit(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    game_id: uuid.UUID,
    human_player_id: str | None,
    decision: Literal["APPLIED", "REFUSED"],
    reason: str,
    flag_enabled: bool,
    created_at: datetime,
) -> RankedWriteAudit | None:
    """Append a ranked write-decision audit row, returning None on conflict."""
    values = {
        "id": uuid.uuid4(),
        "league_id": league_id,
        "game_id": game_id,
        "human_player_id": human_player_id,
        "decision": decision,
        "reason": reason,
        "flag_enabled": flag_enabled,
        "created_at": created_at,
    }
    bind = session.get_bind()
    dialect_name = bind.dialect.name
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = (
            pg_insert(RankedWriteAudit)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(RankedWriteAudit.id)
        )
    elif dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        stmt = (
            sqlite_insert(RankedWriteAudit)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(RankedWriteAudit.id)
        )
    else:
        raise RuntimeError(f"unsupported dialect for ranked-write audit insert: {dialect_name!r}")

    inserted_id = (await session.execute(stmt)).scalar_one_or_none()
    await session.flush()
    if inserted_id is None:
        return None
    inserted = await session.get(RankedWriteAudit, inserted_id)
    if inserted is None:  # pragma: no cover - inserted in the same transaction.
        msg = f"Ranked write-audit row {inserted_id} disappeared after insert"
        raise RuntimeError(msg)
    return inserted
