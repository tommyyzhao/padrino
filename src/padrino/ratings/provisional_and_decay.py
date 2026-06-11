"""Provisional flag, ordinal display, and sigma-decay helpers (US-099).

All public functions are pure: no DB access, no wall-clock, no side effects.
"""

from __future__ import annotations

from datetime import UTC, datetime

DEFAULT_PROVISIONAL_GAMES: int = 10
ORDINAL_BASE: int = 1000
ORDINAL_SCALE: float = 40.0
DEFAULT_DECAY_SIGMA_PER_DAY: float = 0.05
DEFAULT_DECAY_IDLE_DAYS: int = 30


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


__all__ = [
    "DEFAULT_DECAY_IDLE_DAYS",
    "DEFAULT_DECAY_SIGMA_PER_DAY",
    "DEFAULT_PROVISIONAL_GAMES",
    "ORDINAL_BASE",
    "ORDINAL_SCALE",
    "apply_decay",
    "days_idle",
    "is_provisional",
    "to_ordinal",
]
