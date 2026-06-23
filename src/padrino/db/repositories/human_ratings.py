"""CRUD helpers for human-ranked ``HumanRating`` and ``HumanRatingEvent`` rows."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import HumanRating, HumanRatingEvent


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
) -> HumanRatingEvent:
    """Append a human-rating audit row for the given human and scope."""
    obj = HumanRatingEvent(
        league_id=league_id,
        game_id=game_id,
        human_player_id=human_player_id,
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
