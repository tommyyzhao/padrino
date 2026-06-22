"""SOLO_RATE success-rate scoring for non-canonical alt-win contexts."""

from __future__ import annotations

import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import NormalDist
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.enums import RatingContextKind
from padrino.db.models import Game, RatingContext, SoloRateRating, SoloRateRatingEvent
from padrino.db.repositories import rating_contexts as rating_contexts_repo
from padrino.db.repositories import solo_rate_ratings as solo_rate_ratings_repo

SCOPE_ROLE: Final[str] = "ROLE"
DEFAULT_SOLO_RATE_MIN_ATTEMPTS: Final[int] = 10
_PRIOR_ALPHA: Final[float] = 1.0
_PRIOR_BETA: Final[float] = 1.0


@dataclass(frozen=True, slots=True)
class SoloRateAttempt:
    """One role-scoped solo-objective attempt by a public seat."""

    public_player_id: str
    role: str
    succeeded: bool


@dataclass(frozen=True, slots=True)
class SoloRateGameResult:
    """Per-game SOLO_RATE result for one or more solo-objective seats."""

    game_id: uuid.UUID
    outcome_label: str
    attempts: Sequence[SoloRateAttempt]


@dataclass(frozen=True, slots=True)
class SoloRateScore:
    """Read-side score card for a visible SOLO_RATE row."""

    rating_context_id: uuid.UUID
    agent_build_id: uuid.UUID
    scope_type: str
    scope_value: str
    successes: int
    attempts: int
    mean_success_rate: float
    credible_interval_low: float
    credible_interval_high: float


def _posterior(successes: int, attempts: int) -> tuple[float, float, float]:
    if successes < 0 or attempts < 0 or successes > attempts:
        raise ValueError(f"invalid solo-rate counts: successes={successes}, attempts={attempts}")
    alpha = _PRIOR_ALPHA + float(successes)
    beta = _PRIOR_BETA + float(attempts - successes)
    return alpha, beta, alpha / (alpha + beta)


def beta_binomial_credible_interval(
    successes: int,
    attempts: int,
    *,
    confidence: float = 0.95,
) -> tuple[float, float]:
    """Return a normal-approximate Beta posterior credible interval."""
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be between 0 and 1, got {confidence!r}")
    alpha, beta, mean = _posterior(successes, attempts)
    total = alpha + beta
    variance = (alpha * beta) / ((total * total) * (total + 1.0))
    z = NormalDist().inv_cdf(0.5 + confidence / 2.0)
    half_width = z * math.sqrt(variance)
    return max(0.0, mean - half_width), min(1.0, mean + half_width)


async def _resolve_solo_rate_metadata(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
) -> tuple[Game, RatingContext] | None:
    """Resolve a non-canonical SOLO_RATE context for a game, fail-closed."""
    game = await session.get(Game, game_id)
    if game is None:
        return None

    declared = rating_contexts_repo.declared_for_ruleset(game.ruleset_id)
    if declared is not None and (
        declared.kind is not RatingContextKind.SOLO_RATE or declared.is_canonical
    ):
        return None

    context = await rating_contexts_repo.get_by_ruleset_kind(
        session,
        ruleset_id=game.ruleset_id,
        kind=RatingContextKind.SOLO_RATE,
    )
    if context is None:
        return None
    if context.kind != RatingContextKind.SOLO_RATE.value or context.is_canonical:
        return None
    return game, context


def _ordered_attempts(game_result: SoloRateGameResult) -> list[SoloRateAttempt]:
    if not game_result.attempts:
        raise ValueError("solo-rate update requires at least one attempt")

    seen: set[tuple[str, str]] = set()
    attempts: list[SoloRateAttempt] = []
    for attempt in game_result.attempts:
        public_player_id = attempt.public_player_id.strip()
        role = attempt.role.strip()
        if not public_player_id:
            raise ValueError("solo-rate attempt is missing public_player_id")
        if not role:
            raise ValueError(f"solo-rate attempt for {public_player_id} is missing role")
        key = (public_player_id, role)
        if key in seen:
            raise ValueError(f"duplicate solo-rate attempt for {public_player_id}/{role}")
        seen.add(key)
        attempts.append(
            SoloRateAttempt(
                public_player_id=public_player_id,
                role=role,
                succeeded=attempt.succeeded,
            )
        )
    return sorted(attempts, key=lambda item: (item.role, item.public_player_id))


async def _apply_solo_rate_attempt(
    session: AsyncSession,
    *,
    game: Game,
    context: RatingContext,
    outcome_label: str,
    attempt: SoloRateAttempt,
    agent_build_id: uuid.UUID,
    now: datetime,
) -> SoloRateRatingEvent:
    row = await solo_rate_ratings_repo.get_or_create_solo_rate_rating(
        session,
        rating_context_id=context.id,
        agent_build_id=agent_build_id,
        scope_type=SCOPE_ROLE,
        scope_value=attempt.role,
        prior_alpha=_PRIOR_ALPHA,
        prior_beta=_PRIOR_BETA,
    )

    before_successes = int(row.successes)
    before_attempts = int(row.attempts)
    after_successes = before_successes + int(attempt.succeeded)
    after_attempts = before_attempts + 1
    posterior_alpha, posterior_beta, mean = _posterior(after_successes, after_attempts)
    updated = await solo_rate_ratings_repo.update_solo_rate_rating(
        session,
        row.id,
        successes=after_successes,
        attempts=after_attempts,
        posterior_alpha=posterior_alpha,
        posterior_beta=posterior_beta,
        mean_success_rate=mean,
        updated_at=now,
    )
    if updated is None:  # pragma: no cover - row was just inserted in this txn.
        msg = f"SOLO_RATE row {row.id} disappeared between insert and update"
        raise RuntimeError(msg)

    return await solo_rate_ratings_repo.record_solo_rate_rating_event(
        session,
        rating_context_id=context.id,
        game_id=game.id,
        game_seed=game.game_seed,
        outcome_label=outcome_label,
        agent_build_id=updated.agent_build_id,
        scope_type=SCOPE_ROLE,
        scope_value=attempt.role,
        succeeded=attempt.succeeded,
        before_successes=before_successes,
        before_attempts=before_attempts,
        after_successes=after_successes,
        after_attempts=after_attempts,
        public_player_id=attempt.public_player_id,
    )


async def update_solo_rate_ratings_for_game(
    session: AsyncSession,
    *,
    game_result: SoloRateGameResult,
    agent_builds_by_seat: Mapping[str, uuid.UUID],
    now: datetime | None = None,
) -> list[SoloRateRatingEvent]:
    """Apply success/attempt updates for one non-canonical SOLO_RATE game.

    Writes are restricted to the ``solo_rate_ratings`` sibling tables for the
    game's exact non-canonical SOLO_RATE context; missing, canonical, or
    otherwise malformed contexts return no events.
    """
    metadata = await _resolve_solo_rate_metadata(session, game_id=game_result.game_id)
    if metadata is None:
        return []
    game, context = metadata

    _now = now if now is not None else datetime.now(UTC)
    events: list[SoloRateRatingEvent] = []
    for attempt in _ordered_attempts(game_result):
        events.append(
            await _apply_solo_rate_attempt(
                session,
                game=game,
                context=context,
                outcome_label=game_result.outcome_label,
                attempt=attempt,
                agent_build_id=agent_builds_by_seat[attempt.public_player_id],
                now=_now,
            )
        )
    return events


async def list_solo_rate_scores(
    session: AsyncSession,
    *,
    min_attempts: int = DEFAULT_SOLO_RATE_MIN_ATTEMPTS,
    rating_context_id: uuid.UUID | None = None,
    ruleset_id: str | None = None,
) -> list[SoloRateScore]:
    """Return visible SOLO_RATE score cards after the minimum-sample gate."""
    if min_attempts < 1:
        raise ValueError(f"min_attempts must be >= 1, got {min_attempts}")

    stmt = select(SoloRateRating).where(SoloRateRating.attempts >= min_attempts)
    if rating_context_id is not None:
        stmt = stmt.where(SoloRateRating.rating_context_id == rating_context_id)
    if ruleset_id is not None:
        stmt = stmt.join(RatingContext, RatingContext.id == SoloRateRating.rating_context_id).where(
            RatingContext.ruleset_id == ruleset_id,
            RatingContext.kind == RatingContextKind.SOLO_RATE.value,
            RatingContext.is_canonical.is_(False),
        )
    stmt = stmt.order_by(
        SoloRateRating.rating_context_id,
        SoloRateRating.scope_value,
        SoloRateRating.agent_build_id,
    )
    rows = (await session.execute(stmt)).scalars().all()
    scores: list[SoloRateScore] = []
    for row in rows:
        low, high = beta_binomial_credible_interval(row.successes, row.attempts)
        scores.append(
            SoloRateScore(
                rating_context_id=row.rating_context_id,
                agent_build_id=row.agent_build_id,
                scope_type=row.scope_type,
                scope_value=row.scope_value,
                successes=row.successes,
                attempts=row.attempts,
                mean_success_rate=row.mean_success_rate,
                credible_interval_low=low,
                credible_interval_high=high,
            )
        )
    return scores


__all__ = [
    "DEFAULT_SOLO_RATE_MIN_ATTEMPTS",
    "SCOPE_ROLE",
    "SoloRateAttempt",
    "SoloRateGameResult",
    "SoloRateScore",
    "beta_binomial_credible_interval",
    "list_solo_rate_scores",
    "update_solo_rate_ratings_for_game",
]
