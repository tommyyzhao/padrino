"""Admin + public routes for scheduled recurring gauntlets (US-085).

Admin routes (``require_admin``) create / patch / soft-delete schedules. The
public route (``require_public_read``) returns a scrubbed view: no raw cron
expression and no cost cap — only a humanized cadence and run metadata.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.api.auth import ApiKeyContext, require_admin
from padrino.api.deps import get_session
from padrino.api.routes.public import require_public_read
from padrino.core.rulesets import mini7_v1
from padrino.core.scheduling import humanize_cron, next_run_at
from padrino.db.models import AgentBuild, Gauntlet, League
from padrino.db.repositories import scheduled_gauntlets as scheduled_gauntlets_repo

router = APIRouter()

_SEATS = [f"P{i + 1:02d}" for i in range(mini7_v1.PLAYER_COUNT)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RosterSpec(_StrictModel):
    league_id: uuid.UUID
    roster: dict[str, uuid.UUID]


class ScheduledGauntletCreate(_StrictModel):
    name: str = Field(min_length=1, max_length=200)
    schedule_cron: str = Field(min_length=1)
    roster_spec: RosterSpec
    n_games: int = Field(default=1, ge=1, le=100)
    cost_cap_usd: float = Field(gt=0.0)
    enabled: bool = True


class ScheduledGauntletCreateResponse(BaseModel):
    id: uuid.UUID
    next_run_at: datetime | None


class ScheduledGauntletPatch(_StrictModel):
    enabled: bool | None = None
    schedule_cron: str | None = Field(default=None, min_length=1)
    cost_cap_usd: float | None = Field(default=None, gt=0.0)


class ScheduledGauntletPatchResponse(BaseModel):
    id: uuid.UUID
    enabled: bool
    schedule_cron: str
    cost_cap_usd: float
    next_run_at: datetime | None


class ScheduledGauntletDeleteResponse(BaseModel):
    id: uuid.UUID
    enabled: bool


class PublicScheduleEntry(BaseModel):
    name: str
    schedule_cron_human: str
    last_run_at: datetime | None
    next_run_at: datetime | None
    last_gauntlet_id: uuid.UUID | None
    status: str


class PublicSchedulesResponse(BaseModel):
    schedules: list[PublicScheduleEntry]


def _validate_cron(cron_expr: str, *, after: datetime) -> datetime:
    try:
        return next_run_at(cron_expr, after=after)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid schedule_cron: {exc}",
        ) from exc


async def _validate_roster(session: AsyncSession, spec: RosterSpec) -> None:
    if set(spec.roster) != set(_SEATS):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"roster must cover exactly seats {_SEATS}",
        )
    if await session.get(League, spec.league_id) is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown league_id: {spec.league_id}",
        )
    build_ids = set(spec.roster.values())
    found = set(
        (await session.execute(select(AgentBuild.id).where(AgentBuild.id.in_(build_ids))))
        .scalars()
        .all()
    )
    missing = build_ids - found
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown agent_build_id(s): {sorted(str(m) for m in missing)}",
        )


@router.post(
    "/admin/scheduled-gauntlets",
    response_model=ScheduledGauntletCreateResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def create_scheduled_gauntlet(
    body: ScheduledGauntletCreate,
    session: AsyncSession = Depends(get_session),
) -> ScheduledGauntletCreateResponse:
    upcoming = _validate_cron(body.schedule_cron, after=datetime.now(UTC))
    await _validate_roster(session, body.roster_spec)
    if await scheduled_gauntlets_repo.get_by_name(session, body.name) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a schedule named {body.name!r} already exists",
        )
    obj = await scheduled_gauntlets_repo.create(
        session,
        name=body.name,
        schedule_cron=body.schedule_cron,
        roster_spec_json={
            "league_id": str(body.roster_spec.league_id),
            "roster": {seat: str(bid) for seat, bid in body.roster_spec.roster.items()},
        },
        n_games=body.n_games,
        cost_cap_usd=body.cost_cap_usd,
        enabled=body.enabled,
        next_run_at=upcoming if body.enabled else None,
    )
    return ScheduledGauntletCreateResponse(id=obj.id, next_run_at=obj.next_run_at)


@router.patch(
    "/admin/scheduled-gauntlets/{schedule_id}",
    response_model=ScheduledGauntletPatchResponse,
    dependencies=[Depends(require_admin)],
)
async def patch_scheduled_gauntlet(
    schedule_id: uuid.UUID,
    body: ScheduledGauntletPatch,
    session: AsyncSession = Depends(get_session),
) -> ScheduledGauntletPatchResponse:
    existing = await scheduled_gauntlets_repo.get(session, schedule_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    # Recompute next_run_at when the cron or the enabled flag changes.
    recompute = False
    next_value: datetime | None = existing.next_run_at
    effective_cron = (
        body.schedule_cron if body.schedule_cron is not None else existing.schedule_cron
    )
    if body.schedule_cron is not None:
        next_value = _validate_cron(body.schedule_cron, after=datetime.now(UTC))
        recompute = True
    effective_enabled = body.enabled if body.enabled is not None else existing.enabled
    if body.enabled is not None:
        recompute = True
        next_value = (
            _validate_cron(effective_cron, after=datetime.now(UTC)) if effective_enabled else None
        )

    obj = await scheduled_gauntlets_repo.update(
        session,
        schedule_id,
        enabled=body.enabled,
        schedule_cron=body.schedule_cron,
        cost_cap_usd=body.cost_cap_usd,
        next_run_at=next_value,
        set_next_run_at=recompute,
    )
    assert obj is not None
    return ScheduledGauntletPatchResponse(
        id=obj.id,
        enabled=obj.enabled,
        schedule_cron=obj.schedule_cron,
        cost_cap_usd=float(obj.cost_cap_usd),
        next_run_at=obj.next_run_at,
    )


@router.delete(
    "/admin/scheduled-gauntlets/{schedule_id}",
    response_model=ScheduledGauntletDeleteResponse,
    dependencies=[Depends(require_admin)],
)
async def delete_scheduled_gauntlet(
    schedule_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> ScheduledGauntletDeleteResponse:
    obj = await scheduled_gauntlets_repo.disable(session, schedule_id)
    if obj is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return ScheduledGauntletDeleteResponse(id=obj.id, enabled=obj.enabled)


@router.get("/public/scheduled-gauntlets", response_model=PublicSchedulesResponse)
async def public_scheduled_gauntlets(
    _ctx: ApiKeyContext = Depends(require_public_read),
    session: AsyncSession = Depends(get_session),
) -> PublicSchedulesResponse:
    rows = await scheduled_gauntlets_repo.list_all(session)
    # Resolve the status of each schedule's most recent gauntlet (if any).
    gauntlet_ids = [r.last_run_gauntlet_id for r in rows if r.last_run_gauntlet_id is not None]
    status_by_gauntlet: dict[uuid.UUID, str] = {}
    if gauntlet_ids:
        for g in (
            await session.execute(select(Gauntlet).where(Gauntlet.id.in_(gauntlet_ids)))
        ).scalars():
            status_by_gauntlet[g.id] = g.status

    entries: list[PublicScheduleEntry] = []
    for row in rows:
        if not row.enabled:
            sched_status = "disabled"
        elif row.last_run_gauntlet_id is None:
            sched_status = "scheduled"
        else:
            sched_status = status_by_gauntlet.get(row.last_run_gauntlet_id, "unknown")
        entries.append(
            PublicScheduleEntry(
                name=row.name,
                schedule_cron_human=humanize_cron(row.schedule_cron),
                last_run_at=row.last_run_at,
                next_run_at=row.next_run_at,
                last_gauntlet_id=row.last_run_gauntlet_id,
                status=sched_status,
            )
        )
    return PublicSchedulesResponse(schedules=entries)


__all__ = ["router"]
