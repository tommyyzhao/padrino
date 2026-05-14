"""CRUD helpers for :class:`padrino.db.models.Rating` and ``RatingEvent``.

The OpenSkill update pipeline (later story) reads the current rating for
``(league, agent_build, scope)``, applies an update, persists the new value,
and records an audit row in ``rating_events`` with the before/after mu/sigma.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import Rating, RatingEvent


async def get_or_create_rating(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    agent_build_id: uuid.UUID,
    scope_type: str,
    scope_value: str,
    initial_mu: float,
    initial_sigma: float,
    initial_conservative_score: float,
) -> Rating:
    """Return the existing rating row for the scope, or insert a new one."""
    stmt = select(Rating).where(
        Rating.league_id == league_id,
        Rating.agent_build_id == agent_build_id,
        Rating.scope_type == scope_type,
        Rating.scope_value == scope_value,
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return existing

    obj = Rating(
        league_id=league_id,
        agent_build_id=agent_build_id,
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


async def update_rating(
    session: AsyncSession,
    rating_id: uuid.UUID,
    *,
    mu: float,
    sigma: float,
    conservative_score: float,
    games: int,
    updated_at: datetime | None = None,
) -> Rating | None:
    """Update the mu/sigma/conservative_score/games on an existing rating row."""
    rating = await session.get(Rating, rating_id)
    if rating is None:
        return None
    rating.mu = mu
    rating.sigma = sigma
    rating.conservative_score = conservative_score
    rating.games = games
    if updated_at is not None:
        rating.updated_at = updated_at
    await session.flush()
    return rating


async def record_rating_event(
    session: AsyncSession,
    *,
    league_id: uuid.UUID,
    game_id: uuid.UUID,
    agent_build_id: uuid.UUID,
    scope_type: str,
    scope_value: str,
    before_mu: float,
    before_sigma: float,
    after_mu: float,
    after_sigma: float,
) -> RatingEvent:
    """Append a rating-event audit row for the given (league, game, build, scope)."""
    obj = RatingEvent(
        league_id=league_id,
        game_id=game_id,
        agent_build_id=agent_build_id,
        scope_type=scope_type,
        scope_value=scope_value,
        before_mu=before_mu,
        before_sigma=before_sigma,
        after_mu=after_mu,
        after_sigma=after_sigma,
    )
    session.add(obj)
    await session.flush()
    return obj


async def list_rating_events(
    session: AsyncSession,
    *,
    league_id: uuid.UUID | None = None,
    game_id: uuid.UUID | None = None,
    agent_build_id: uuid.UUID | None = None,
) -> list[RatingEvent]:
    """Return rating-event audit rows filtered by any combination of scopes."""
    stmt = select(RatingEvent)
    if league_id is not None:
        stmt = stmt.where(RatingEvent.league_id == league_id)
    if game_id is not None:
        stmt = stmt.where(RatingEvent.game_id == game_id)
    if agent_build_id is not None:
        stmt = stmt.where(RatingEvent.agent_build_id == agent_build_id)
    stmt = stmt.order_by(RatingEvent.created_at, RatingEvent.id)
    result = await session.execute(stmt)
    return list(result.scalars())
