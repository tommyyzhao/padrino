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

from datetime import UTC, datetime, timedelta
from typing import Final

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.api.auth import _get_auth_settings
from padrino.api.deps import get_session
from padrino.db.models import (
    OperatorCompletionReserveLedgerEntry,
    PromotionalCreditLedgerEntry,
    PromotionalCreditReservation,
)
from padrino.db.repositories import gauntlets as gauntlets_repo
from padrino.db.repositories import model_catalog_snapshots as catalog_snapshots_repo
from padrino.db.repositories import scheduler_heartbeats as scheduler_heartbeats_repo
from padrino.openrouter.credentials import credential_encryption_available
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


class OpenRouterFundingHealthResponse(BaseModel):
    """Safe OpenRouter promotional-funding capability summary."""

    mode: str
    management_configured: bool
    management_available: bool
    inference_configured: bool
    inference_available: bool
    encryption_available: bool
    upstream_allowlist_enabled: bool


class OpenRouterCatalogHealthResponse(BaseModel):
    """Safe persisted-catalog freshness facts for operational monitoring."""

    content_hash: str | None
    fetched_at: datetime | None
    state: str
    is_stale: bool


class OpenRouterManagementHealthResponse(BaseModel):
    """Capability booleans for the separately scoped management integration."""

    configured: bool
    available: bool
    mode: str


class PromotionalCreditOperationsHealthResponse(BaseModel):
    """Aggregate user-credit exposure, never a per-principal wallet projection."""

    liability_micros: int
    active_reservation_micros: int
    active_reservation_count: int


class CompletionReserveOperationsHealthResponse(BaseModel):
    """Aggregate operator completion-reserve facts without model or player attribution."""

    global_cap_micros: int
    charged_micros: int
    remaining_micros: int


class OpenRouterOperationsHealthResponse(BaseModel):
    """Secret-free aggregate OpenRouter readiness and funding status."""

    catalog: OpenRouterCatalogHealthResponse
    encryption_ready: bool
    management: OpenRouterManagementHealthResponse
    promotional_credit: PromotionalCreditOperationsHealthResponse
    completion_reserve: CompletionReserveOperationsHealthResponse


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


@router.get("/healthz/openrouter-funding", response_model=OpenRouterFundingHealthResponse)
async def healthz_openrouter_funding(request: Request) -> OpenRouterFundingHealthResponse:
    """Expose the explicit subkey/fallback mode without exposing credential details."""
    from padrino.openrouter.platform_subkeys import platform_subkey_health

    summary = platform_subkey_health(_get_auth_settings(request))
    return OpenRouterFundingHealthResponse(
        mode=summary.mode,
        management_configured=summary.management_configured,
        management_available=summary.management_available,
        inference_configured=summary.inference_configured,
        inference_available=summary.inference_available,
        encryption_available=summary.encryption_available,
        upstream_allowlist_enabled=summary.upstream_allowlist_enabled,
    )


@router.get("/healthz/openrouter-operations", response_model=OpenRouterOperationsHealthResponse)
async def healthz_openrouter_operations(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> OpenRouterOperationsHealthResponse:
    """Report aggregate OpenRouter operations facts without PII or secrets."""
    settings = _get_auth_settings(request)
    from padrino.openrouter.platform_subkeys import platform_subkey_health

    snapshot = await catalog_snapshots_repo.get_latest_complete_snapshot(session)
    now = datetime.now(UTC)
    if snapshot is None:
        catalog = OpenRouterCatalogHealthResponse(
            content_hash=None,
            fetched_at=None,
            state="unavailable",
            is_stale=True,
        )
    else:
        fetched_at = (
            snapshot.fetched_at.replace(tzinfo=UTC)
            if snapshot.fetched_at.tzinfo is None
            else snapshot.fetched_at.astimezone(UTC)
        )
        is_stale = now > fetched_at + timedelta(
            seconds=settings.padrino_openrouter_catalog_sync_interval_seconds
        )
        catalog = OpenRouterCatalogHealthResponse(
            content_hash=snapshot.content_hash,
            fetched_at=fetched_at,
            state="stale" if is_stale else "current",
            is_stale=is_stale,
        )
    liability = await session.scalar(
        select(func.coalesce(func.sum(PromotionalCreditLedgerEntry.amount_micros), 0))
    )
    active_reservation_micros = await session.scalar(
        select(
            func.coalesce(
                func.sum(
                    PromotionalCreditReservation.reserved_micros
                    - PromotionalCreditReservation.consumed_micros
                    - PromotionalCreditReservation.released_micros
                ),
                0,
            )
        ).where(PromotionalCreditReservation.status == "ACTIVE")
    )
    active_reservation_count = await session.scalar(
        select(func.count())
        .select_from(PromotionalCreditReservation)
        .where(PromotionalCreditReservation.status == "ACTIVE")
    )
    charged_micros = await session.scalar(
        select(
            func.coalesce(func.sum(-OperatorCompletionReserveLedgerEntry.amount_micros), 0)
        ).where(OperatorCompletionReserveLedgerEntry.entry_type == "CHARGE")
    )
    global_cap = settings.padrino_openrouter_completion_reserve_global_micros
    charged = int(charged_micros or 0)
    funding = platform_subkey_health(settings)
    return OpenRouterOperationsHealthResponse(
        catalog=catalog,
        encryption_ready=credential_encryption_available(settings),
        management=OpenRouterManagementHealthResponse(
            configured=funding.management_configured,
            available=funding.management_available,
            mode=funding.mode,
        ),
        promotional_credit=PromotionalCreditOperationsHealthResponse(
            liability_micros=int(liability or 0),
            active_reservation_micros=int(active_reservation_micros or 0),
            active_reservation_count=int(active_reservation_count or 0),
        ),
        completion_reserve=CompletionReserveOperationsHealthResponse(
            global_cap_micros=global_cap,
            charged_micros=charged,
            remaining_micros=max(0, global_cap - charged),
        ),
    )


__all__ = [
    "HEARTBEAT_DOWN_THRESHOLD_S",
    "PENDING_DEGRADED_THRESHOLD_S",
    "HumanLaneHealthResponse",
    "OpenRouterCatalogHealthResponse",
    "OpenRouterFundingHealthResponse",
    "OpenRouterOperationsHealthResponse",
    "SchedulerHealthResponse",
    "router",
]
