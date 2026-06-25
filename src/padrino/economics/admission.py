"""Admission / queue policy: daily and concurrency caps (US-096).

Combines the spend cap (US-095) with a daily game count limit and a
concurrency limit.  All three must pass for ``admit()`` to return
``AdmitDecision(allowed=True)``.

Denial reasons are typed strings so callers can branch on them without
string comparison:

    ``"spend_cap_reached"``     — cumulative spend >= global cap
    ``"daily_cap_reached"``     — games started today >= max_games_per_day
    ``"concurrency_cap_reached"`` — active (non-terminal) games >= max_concurrent_games
    ``"admitted"``              — all checks passed
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.game_status import GAME_TERMINAL_STATUSES
from padrino.db.models import Game
from padrino.economics.spend_governor import can_start_game
from padrino.settings import Settings

_logger = structlog.get_logger("padrino.economics.admission")


@dataclasses.dataclass(frozen=True)
class AdmitDecision:
    """Typed result of an admission check."""

    allowed: bool
    reason: str


async def admit(
    session: AsyncSession,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> AdmitDecision:
    """Return an :class:`AdmitDecision` combining spend, daily, and concurrency checks.

    Checks are evaluated in priority order: spend cap → daily cap → concurrency
    cap.  The first failing check short-circuits; denials are logged with a
    structured ``admission.denied`` event.

    The *now* parameter is injectable for deterministic testing; when omitted it
    defaults to ``datetime.now(tz=timezone.utc)``.
    """
    if now is None:
        now = datetime.now(tz=UTC)

    # 1. Spend cap (delegates to US-095 governor)
    if not await can_start_game(session, settings):
        _logger.warning("admission.denied", reason="spend_cap_reached")
        return AdmitDecision(allowed=False, reason="spend_cap_reached")

    # 2. Daily game count: games whose created_at falls in [today_start, tomorrow_start)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_start = today_start + timedelta(days=1)

    stmt_daily = select(func.count(Game.id)).where(
        Game.created_at >= today_start,
        Game.created_at < tomorrow_start,
    )
    daily_count: int = (await session.execute(stmt_daily)).scalar_one()

    if daily_count >= settings.padrino_max_games_per_day:
        _logger.warning(
            "admission.denied",
            reason="daily_cap_reached",
            daily_count=daily_count,
            max_games_per_day=settings.padrino_max_games_per_day,
        )
        return AdmitDecision(allowed=False, reason="daily_cap_reached")

    # 3. Concurrency: only non-terminal games are considered active.
    stmt_concurrent = select(func.count(Game.id)).where(~Game.status.in_(GAME_TERMINAL_STATUSES))
    concurrent_count: int = (await session.execute(stmt_concurrent)).scalar_one()

    if concurrent_count >= settings.padrino_max_concurrent_games:
        _logger.warning(
            "admission.denied",
            reason="concurrency_cap_reached",
            concurrent_count=concurrent_count,
            max_concurrent_games=settings.padrino_max_concurrent_games,
        )
        return AdmitDecision(allowed=False, reason="concurrency_cap_reached")

    _logger.info("admission.allowed", reason="admitted")
    return AdmitDecision(allowed=True, reason="admitted")
