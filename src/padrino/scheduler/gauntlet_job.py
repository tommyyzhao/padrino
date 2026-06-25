"""Scheduled-gauntlet job (US-085).

On each scheduler tick :func:`run_due_scheduled_gauntlets` fires every enabled
``scheduled_gauntlets`` row whose ``next_run_at`` is due: it materializes the
serialized roster spec, runs an N-game heterogeneous tournament under the row's
cost cap, records the produced gauntlet, and recomputes ``next_run_at`` from the
cron expression. A cost-cap overrun leaves the partial gauntlet row with
``status='cost_capped'``; a clean run is finalized to ``COMPLETED``.

Impure module: reads the DB and (indirectly) calls providers. ``now`` is
injected so the scheduler's clock seam (and tests) drive timing deterministically.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.scheduling import next_run_at
from padrino.db.models import Gauntlet
from padrino.db.repositories import scheduled_gauntlets as scheduled_gauntlets_repo
from padrino.gauntlets.completion import finalize_gauntlet_if_done
from padrino.gauntlets.tournament import AdapterFactory, run_tournament_from_roster
from padrino.settings import Settings

_logger = structlog.get_logger("padrino.scheduler.gauntlet_job")

# Status stamped on a gauntlet whose run was aborted mid-way by the cost cap.
STATUS_COST_CAPPED = "cost_capped"


@dataclass(frozen=True, slots=True)
class ScheduledRun:
    schedule_id: uuid.UUID
    gauntlet_id: uuid.UUID
    games_run: int
    cost_capped: bool
    total_cost_usd: float


def _roster_from_spec(spec: dict[str, object]) -> tuple[uuid.UUID, dict[str, uuid.UUID]]:
    league_raw = spec.get("league_id")
    roster_raw = spec.get("roster")
    if not isinstance(league_raw, str):
        raise ValueError("roster_spec_json missing string 'league_id'")
    if not isinstance(roster_raw, dict):
        raise ValueError("roster_spec_json missing 'roster' mapping")
    league_id = uuid.UUID(league_raw)
    roster_by_seat = {str(seat): uuid.UUID(str(bid)) for seat, bid in roster_raw.items()}
    return league_id, roster_by_seat


def _scheduled_fire_key(scheduled_fire_at: datetime | None) -> str:
    if scheduled_fire_at is None:
        return "initial"
    if scheduled_fire_at.tzinfo is None:
        scheduled_fire_at = scheduled_fire_at.replace(tzinfo=UTC)
    else:
        scheduled_fire_at = scheduled_fire_at.astimezone(UTC)
    return scheduled_fire_at.isoformat()


def derive_scheduled_gauntlet_seed(
    schedule_id: uuid.UUID,
    *,
    scheduled_fire_at: datetime | None,
) -> str:
    """Derive a scheduled gauntlet seed from stable schedule occurrence data."""
    return hashlib.sha256(
        b"scheduled:"
        + str(schedule_id).encode("utf-8")
        + b":"
        + _scheduled_fire_key(scheduled_fire_at).encode("utf-8")
    ).hexdigest()


async def run_due_scheduled_gauntlets(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime,
    settings: Settings,
    adapter_factory: AdapterFactory | None = None,
) -> list[ScheduledRun]:
    """Run every due scheduled gauntlet and return what fired.

    Each schedule runs in isolation: a failure in one is logged and skipped so
    the others still fire.
    """
    async with session_factory() as session:
        due = await scheduled_gauntlets_repo.list_due(session, now=now)

    runs: list[ScheduledRun] = []
    for row in due:
        schedule_id = row.id
        scheduled_fire_at = row.next_run_at
        try:
            league_id, roster_by_seat = _roster_from_spec(row.roster_spec_json)
            gauntlet_id, result = await run_tournament_from_roster(
                session_factory=session_factory,
                league_id=league_id,
                gauntlet_seed=derive_scheduled_gauntlet_seed(
                    schedule_id,
                    scheduled_fire_at=scheduled_fire_at,
                ),
                roster_by_seat=roster_by_seat,
                n_games=row.n_games,
                settings=settings,
                cost_cap_usd=float(row.cost_cap_usd),
                adapter_factory=adapter_factory,
            )

            if result.cost_capped:
                async with session_factory() as session, session.begin():
                    gauntlet = await session.get(Gauntlet, gauntlet_id)
                    if gauntlet is not None:
                        gauntlet.status = STATUS_COST_CAPPED
            else:
                # finalize_gauntlet_if_done manages its own transaction/commit.
                async with session_factory() as session:
                    await finalize_gauntlet_if_done(session, gauntlet_id)

            upcoming = next_run_at(row.schedule_cron, after=now)
            async with session_factory() as session, session.begin():
                await scheduled_gauntlets_repo.mark_run(
                    session,
                    schedule_id,
                    last_run_at=now,
                    last_run_gauntlet_id=gauntlet_id,
                    next_run_at=upcoming,
                )

            _logger.info(
                "scheduler.gauntlet_job.ran",
                schedule_id=str(schedule_id),
                gauntlet_id=str(gauntlet_id),
                games_run=result.games_run,
                cost_capped=result.cost_capped,
                next_run_at=upcoming.isoformat(),
            )
            runs.append(
                ScheduledRun(
                    schedule_id=schedule_id,
                    gauntlet_id=gauntlet_id,
                    games_run=result.games_run,
                    cost_capped=result.cost_capped,
                    total_cost_usd=result.total_cost_usd,
                )
            )
        except Exception:
            _logger.exception(
                "scheduler.gauntlet_job.failed",
                schedule_id=str(schedule_id),
            )
    return runs


__all__ = [
    "STATUS_COST_CAPPED",
    "ScheduledRun",
    "derive_scheduled_gauntlet_seed",
    "run_due_scheduled_gauntlets",
]
