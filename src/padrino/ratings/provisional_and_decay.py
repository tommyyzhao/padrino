"""Provisional flag, ordinal display, and sigma-decay helpers (US-099).

All public functions are pure: no DB access, no wall-clock, no side effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

DEFAULT_PROVISIONAL_GAMES: int = 10
ORDINAL_BASE: int = 1000
ORDINAL_SCALE: float = 40.0
DEFAULT_DECAY_SIGMA_PER_DAY: float = 0.05
DEFAULT_DECAY_IDLE_DAYS: int = 30
# Human ranked ratings use a shorter read-side grace window than the model
# ladder default; reads project uncertainty without mutating stored rows.
HUMAN_RATING_DECAY_IDLE_DAYS: int = 14


@dataclass(frozen=True, slots=True)
class DecayedRatingDisplay:
    """Read-side rating values after optional sigma decay projection."""

    mu: float
    sigma: float
    conservative_score: float


def is_provisional(games: int, *, threshold: int = DEFAULT_PROVISIONAL_GAMES) -> bool:
    """Return True when the agent has fewer than *threshold* rated games."""
    return games < threshold


def to_ordinal(mu: float, sigma: float) -> int:
    """Map OpenSkill (mu, sigma) to an ELO-style display integer.

    Uses ``mu - 3*sigma`` (conservative score) scaled and offset so that
    a newly-rated agent (conservative_score ≈ 0) starts at ORDINAL_BASE (1000).
    """
    return round(ORDINAL_BASE + (mu - 3.0 * sigma) * ORDINAL_SCALE)


def _aware(dt: datetime) -> datetime:
    """Coerce a naive datetime (e.g. from aiosqlite) to UTC for comparison."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def days_idle(last_game_at: datetime | None, *, now: datetime) -> int:
    """Return whole days elapsed since *last_game_at*, or 0 if never played.

    Both operands are normalized to aware UTC: SQLite drivers return naive
    datetimes for ``DateTime(timezone=True)`` columns while callers pass
    ``datetime.now(UTC)`` — mixing the two would raise TypeError.
    """
    if last_game_at is None:
        return 0
    return max(0, (_aware(now) - _aware(last_game_at)).days)


def apply_decay(
    sigma: float,
    idle_days: int,
    *,
    decay_per_day: float = DEFAULT_DECAY_SIGMA_PER_DAY,
) -> float:
    """Return the inflated sigma after *idle_days* of inactivity.

    Linear growth model: ``sigma_new = sigma * (1 + decay_per_day * idle_days)``.
    When ``idle_days <= 0`` the sigma is returned unchanged.
    """
    if idle_days <= 0:
        return sigma
    return sigma * (1.0 + decay_per_day * idle_days)


def project_decayed_display(
    mu: float,
    sigma: float,
    last_game_at: datetime | None,
    *,
    now: datetime,
    idle_threshold_days: int = HUMAN_RATING_DECAY_IDLE_DAYS,
    decay_per_day: float = DEFAULT_DECAY_SIGMA_PER_DAY,
) -> DecayedRatingDisplay:
    """Return read-side display values with stale-rating sigma inflation.

    ``now`` is injected by the caller so this helper stays pure. Only idle days
    beyond ``idle_threshold_days`` are decayed, and the projection never makes a
    stale rating look better than its persisted ``mu - 3*sigma`` score.
    """
    idle = days_idle(last_game_at, now=now)
    effective_idle = max(0, idle - idle_threshold_days)
    decayed_sigma = apply_decay(sigma, effective_idle, decay_per_day=decay_per_day)
    display_sigma = max(sigma, decayed_sigma)
    return DecayedRatingDisplay(
        mu=mu,
        sigma=display_sigma,
        conservative_score=mu - 3.0 * display_sigma,
    )


__all__ = [
    "DEFAULT_DECAY_IDLE_DAYS",
    "DEFAULT_DECAY_SIGMA_PER_DAY",
    "DEFAULT_PROVISIONAL_GAMES",
    "HUMAN_RATING_DECAY_IDLE_DAYS",
    "ORDINAL_BASE",
    "ORDINAL_SCALE",
    "DecayedRatingDisplay",
    "apply_decay",
    "days_idle",
    "is_provisional",
    "project_decayed_display",
    "to_ordinal",
]
