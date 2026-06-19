"""Scheduler bootstrap: register recurring jobs as scheduler tick hooks (US-085).

``run_scheduler`` calls a single ``tick_hook`` once per loop iteration with the
current clock time. :func:`build_scheduled_gauntlet_tick_hook` returns the hook
that fires due ``scheduled_gauntlets`` rows, threading the injected clock so the
job's timing stays deterministic under test.

US-098 extends the hook to also run the continuous matchmaking pipeline when
``padrino_enable_continuous_matchmaking`` is True.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.gauntlets.tournament import AdapterFactory
from padrino.observability.alerts import AlertNotifier
from padrino.public.moderation import GuardModelAdapter
from padrino.scheduler.gauntlet_job import run_due_scheduled_gauntlets
from padrino.settings import Settings

TickHook = Callable[[datetime], Awaitable[None]]


def build_scheduled_gauntlet_tick_hook(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: Settings,
    adapter_factory: AdapterFactory | None = None,
    guard: GuardModelAdapter | None = None,
    notifier: AlertNotifier | None = None,
) -> TickHook:
    """Return a scheduler tick hook that fires every due scheduled gauntlet.

    When ``padrino_enable_continuous_matchmaking`` is True the hook also runs
    the continuous matchmaking pipeline (admission → matchmaker → runner →
    moderation gate) on each tick.
    """

    async def _hook(now: datetime) -> None:
        await run_due_scheduled_gauntlets(
            session_factory,
            now=now,
            settings=settings,
            adapter_factory=adapter_factory,
        )
        if settings.padrino_enable_behavioral_evaluation:
            from padrino.ratings.evaluator import run_pending_behavioral_evaluations

            await run_pending_behavioral_evaluations(
                session_factory,
                settings=settings,
            )
        if settings.padrino_enable_continuous_matchmaking:
            from padrino.scheduler.continuous_matchmaking import (
                run_continuous_matchmaking_tick,
            )

            await run_continuous_matchmaking_tick(
                session_factory,
                settings=settings,
                now=now,
                guard=guard,
                adapter_factory=adapter_factory,
                notifier=notifier,
            )

    return _hook


__all__ = ["TickHook", "build_scheduled_gauntlet_tick_hook"]
