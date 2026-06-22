"""CRUD helpers for non-canonical placement rating rows."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import PlacementRating, PlacementRatingEvent


async def get_or_create_placement_rating(
    session: AsyncSession,
    *,
    rating_context_id: uuid.UUID,
    agent_build_id: uuid.UUID,
    scope_type: str,
    scope_value: str,
    initial_mu: float,
    initial_sigma: float,
    initial_conservative_score: float,
) -> PlacementRating:
    """Return the existing placement rating row for the scope, or insert one."""
    stmt = select(PlacementRating).where(
        PlacementRating.rating_context_id == rating_context_id,
        PlacementRating.agent_build_id == agent_build_id,
        PlacementRating.scope_type == scope_type,
        PlacementRating.scope_value == scope_value,
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return existing

    obj = PlacementRating(
        rating_context_id=rating_context_id,
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


async def update_placement_rating(
    session: AsyncSession,
    rating_id: uuid.UUID,
    *,
    mu: float,
    sigma: float,
    conservative_score: float,
    games: int,
    updated_at: datetime | None = None,
    last_game_at: datetime | None = None,
) -> PlacementRating | None:
    """Update an existing placement rating row."""
    rating = await session.get(PlacementRating, rating_id)
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


async def record_placement_rating_event(
    session: AsyncSession,
    *,
    rating_context_id: uuid.UUID,
    game_id: uuid.UUID,
    game_seed: str,
    team_outcome: str,
    agent_build_id: uuid.UUID,
    scope_type: str,
    scope_value: str,
    before_mu: float,
    before_sigma: float,
    after_mu: float,
    after_sigma: float,
    public_player_id: str | None = None,
) -> PlacementRatingEvent:
    """Append a placement-rating audit row."""
    obj = PlacementRatingEvent(
        rating_context_id=rating_context_id,
        game_id=game_id,
        game_seed=game_seed,
        team_outcome=team_outcome,
        agent_build_id=agent_build_id,
        public_player_id=public_player_id,
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


__all__ = [
    "get_or_create_placement_rating",
    "record_placement_rating_event",
    "update_placement_rating",
]
