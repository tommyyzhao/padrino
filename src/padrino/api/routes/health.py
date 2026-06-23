"""Worker readiness endpoints (US-060, US-230).

``GET /healthz/scheduler`` surfaces the scheduler's last per-worker
heartbeat, queue depth, and oldest pending age so operators can alert on a
stuck worker without scraping logs. The existing ``/healthz`` route in
:mod:`padrino.api.app` remains the cheap process-liveness probe.

``GET /healthz/human-lane`` mirrors that pattern for the separate human-game
worker lane: the worker writes a DB heartbeat and this API process reports lane
liveness plus coarse queue counts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.api.deps import get_session
from padrino.db.repositories import gauntlets as gauntlets_repo
from padrino.db.repositories import scheduler_heartbeats as scheduler_heartbeats_repo
from padrino.runner.human_lane import list_human_lane_games

router = APIRouter()

HEARTBEAT_DOWN_THRESHOLD_S: Final[float] = 30.0
PENDING_DEGRADED_THRESHOLD_S: Final[float] = 60.0


class SchedulerHealthResponse(BaseModel):
    status: str
    last_heartbeat_at: datetime | None
    pending_gauntlets: int
    running_gauntlets: int
    oldest_pending_age_s: float | None


class HumanLaneHealthResponse(BaseModel):
    status: str
    last_heartbeat_at: datetime | None
    waiting_games: int
    running_games: int


def _heartbeat_liveness_status(
    *,
    last_heartbeat_at: datetime | None,
    now: datetime,
) -> str:
    if last_heartbeat_at is None:
        return "down"
    age_s = (now - last_heartbeat_at).total_seconds()
    if age_s > HEARTBEAT_DOWN_THRESHOLD_S:
        return "down"
    return "ok"


def _classify(
    *,
    last_heartbeat_at: datetime | None,
    oldest_pending_age_s: float | None,
    now: datetime,
) -> str:
    if last_heartbeat_at is None:
        return "down"
    age_s = (now - last_heartbeat_at).total_seconds()
    if age_s > HEARTBEAT_DOWN_THRESHOLD_S:
        return "down"
    if oldest_pending_age_s is not None and oldest_pending_age_s > PENDING_DEGRADED_THRESHOLD_S:
        return "degraded"
    return "ok"


@router.get("/healthz/scheduler", response_model=SchedulerHealthResponse)
async def healthz_scheduler(
    session: AsyncSession = Depends(get_session),
) -> SchedulerHealthResponse:
    last_heartbeat_at = await scheduler_heartbeats_repo.latest_scheduler_beat(session)
    pending_count = await gauntlets_repo.count_by_status(session, "PENDING")
    running_count = await gauntlets_repo.count_by_status(session, "RUNNING")
    oldest_pending_created_at = await gauntlets_repo.oldest_pending_created_at(session)

    now = datetime.now(UTC)
    oldest_pending_age_s: float | None = None
    if oldest_pending_created_at is not None:
        oldest_pending_age_s = (now - oldest_pending_created_at).total_seconds()

    status_value = _classify(
        last_heartbeat_at=last_heartbeat_at,
        oldest_pending_age_s=oldest_pending_age_s,
        now=now,
    )

    return SchedulerHealthResponse(
        status=status_value,
        last_heartbeat_at=last_heartbeat_at,
        pending_gauntlets=pending_count,
        running_gauntlets=running_count,
        oldest_pending_age_s=oldest_pending_age_s,
    )


@router.get("/healthz/human-lane", response_model=HumanLaneHealthResponse)
async def healthz_human_lane(
    session: AsyncSession = Depends(get_session),
) -> HumanLaneHealthResponse:
    last_heartbeat_at = await scheduler_heartbeats_repo.latest_human_lane_beat(session)
    waiting = await list_human_lane_games(session, statuses=frozenset({"CREATED", "PENDING"}))
    running = await list_human_lane_games(session, statuses=frozenset({"RUNNING"}))
    now = datetime.now(UTC)

    return HumanLaneHealthResponse(
        status=_heartbeat_liveness_status(last_heartbeat_at=last_heartbeat_at, now=now),
        last_heartbeat_at=last_heartbeat_at,
        waiting_games=len(waiting),
        running_games=len(running),
    )


__all__ = [
    "HEARTBEAT_DOWN_THRESHOLD_S",
    "PENDING_DEGRADED_THRESHOLD_S",
    "HumanLaneHealthResponse",
    "SchedulerHealthResponse",
    "router",
]
