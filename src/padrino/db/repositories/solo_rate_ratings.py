"""CRUD helpers for non-canonical SOLO_RATE rating rows."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import SoloRateRating, SoloRateRatingEvent


async def get_or_create_solo_rate_rating(
    session: AsyncSession,
    *,
    rating_context_id: uuid.UUID,
    agent_build_id: uuid.UUID,
    scope_type: str,
    scope_value: str,
    prior_alpha: float,
    prior_beta: float,
) -> SoloRateRating:
    """Return the existing SOLO_RATE row for the scope, or insert one."""
    stmt = select(SoloRateRating).where(
        SoloRateRating.rating_context_id == rating_context_id,
        SoloRateRating.agent_build_id == agent_build_id,
        SoloRateRating.scope_type == scope_type,
        SoloRateRating.scope_value == scope_value,
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return existing

    obj = SoloRateRating(
        rating_context_id=rating_context_id,
        agent_build_id=agent_build_id,
        scope_type=scope_type,
        scope_value=scope_value,
        successes=0,
        attempts=0,
        posterior_alpha=prior_alpha,
        posterior_beta=prior_beta,
        mean_success_rate=prior_alpha / (prior_alpha + prior_beta),
    )
    session.add(obj)
    await session.flush()
    return obj


async def update_solo_rate_rating(
    session: AsyncSession,
    rating_id: uuid.UUID,
    *,
    successes: int,
    attempts: int,
    posterior_alpha: float,
    posterior_beta: float,
    mean_success_rate: float,
    updated_at: datetime | None = None,
) -> SoloRateRating | None:
    """Update an existing SOLO_RATE row."""
    rating = await session.get(SoloRateRating, rating_id)
    if rating is None:
        return None
    rating.successes = successes
    rating.attempts = attempts
    rating.posterior_alpha = posterior_alpha
    rating.posterior_beta = posterior_beta
    rating.mean_success_rate = mean_success_rate
    if updated_at is not None:
        rating.updated_at = updated_at
    await session.flush()
    return rating


async def record_solo_rate_rating_event(
    session: AsyncSession,
    *,
    rating_context_id: uuid.UUID,
    game_id: uuid.UUID,
    game_seed: str,
    outcome_label: str,
    agent_build_id: uuid.UUID,
    scope_type: str,
    scope_value: str,
    succeeded: bool,
    before_successes: int,
    before_attempts: int,
    after_successes: int,
    after_attempts: int,
    public_player_id: str | None = None,
) -> SoloRateRatingEvent:
    """Append a SOLO_RATE audit row."""
    obj = SoloRateRatingEvent(
        rating_context_id=rating_context_id,
        game_id=game_id,
        game_seed=game_seed,
        outcome_label=outcome_label,
        agent_build_id=agent_build_id,
        public_player_id=public_player_id,
        scope_type=scope_type,
        scope_value=scope_value,
        succeeded=succeeded,
        before_successes=before_successes,
        before_attempts=before_attempts,
        after_successes=after_successes,
        after_attempts=after_attempts,
    )
    session.add(obj)
    await session.flush()
    return obj


__all__ = [
    "get_or_create_solo_rate_rating",
    "record_solo_rate_rating_event",
    "update_solo_rate_rating",
]
