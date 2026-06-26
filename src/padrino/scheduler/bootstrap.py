"""Scheduler bootstrap: register recurring jobs as scheduler tick hooks (US-085).

``run_scheduler`` calls a single ``tick_hook`` once per loop iteration with the
current clock time. :func:`build_scheduled_gauntlet_tick_hook` returns the hook
that fires due ``scheduled_gauntlets`` rows, threading the injected clock so the
job's timing stays deterministic under test.

US-098 extends the hook to also run the continuous matchmaking pipeline when
``padrino_enable_continuous_matchmaking`` is True. US-262 adds the gated
campaign tick to the same composed hook.
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
    worker_id: str | None = None,
) -> TickHook:
    """Return a scheduler tick hook that fires every due scheduled gauntlet.

    When ``padrino_enable_continuous_matchmaking`` is True the hook also runs
    the continuous matchmaking pipeline (admission → matchmaker → runner →
    moderation gate) on each tick.
    """
    campaign_worker_id = worker_id
    if campaign_worker_id is None:
        from padrino.runner.scheduler import default_worker_id

        campaign_worker_id = default_worker_id()

    async def _hook(now: datetime) -> None:
        await run_due_scheduled_gauntlets(
            session_factory,
            now=now,
            settings=settings,
            adapter_factory=adapter_factory,
        )
        if settings.padrino_enable_campaign_tick:
            from padrino.scheduler.campaign_tick import run_campaign_tick

            await run_campaign_tick(
                session_factory,
                now=now,
                settings=settings,
                worker_id=campaign_worker_id,
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
        if settings.padrino_enable_retention:
            from padrino.db.retention_executor import run_retention_executor

            await run_retention_executor(
                session_factory,
                settings=settings,
                now=now,
            )

    return _hook


__all__ = ["TickHook", "build_scheduled_gauntlet_tick_hook"]
