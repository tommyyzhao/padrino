"""Admin read-only routes for ranked anti-cheat review."""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.api.auth import require_admin
from padrino.api.deps import get_session
from padrino.api.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MIN_LIMIT,
    CursorPage,
    paginate_keyset,
)
from padrino.db.models import Principal, RankedFlag, RankedGameSignal
from padrino.db.repositories.human_principals import derive_default_ranked_handle

router = APIRouter(prefix="/admin/anticheat", dependencies=[Depends(require_admin)])

_RAW_IP_KEYS = frozenset(
    {
        "client_ip",
        "ip",
        "ip_address",
        "raw_ip",
        "remote_addr",
        "source_ip",
    }
)
_REDACTED = "[redacted]"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnticheatFlagListQuery(_StrictModel):
    limit: int = Field(default=DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT)
    cursor: str | None = None
    kind: str | None = None
    severity: str | None = None
    principal_id: uuid.UUID | None = None


class AnticheatFlagResponse(BaseModel):
    id: uuid.UUID
    kind: str
    principal_id: str
    ranked_handle: str
    severity: str
    evidence: dict[str, Any]
    evidence_hash: str
    created_at: datetime


class PrincipalAnticheatRollup(BaseModel):
    principal_id: uuid.UUID
    ranked_handle: str
    ranked_game_count: int
    distinct_source_ip_hash_count: int
    win_rate: float | None
    flags: list[AnticheatFlagResponse]


class RankedGameSignalResponse(BaseModel):
    id: uuid.UUID
    game_id: uuid.UUID
    human_principal_id: uuid.UUID
    ranked_handle: str
    source_ip_hash: str
    session_fingerprint_hash: str | None
    winner: str
    faction: str
    signals: dict[str, Any]
    created_at: datetime


class RankedGameSignalsResponse(BaseModel):
    game_id: uuid.UUID
    items: list[RankedGameSignalResponse]


def _is_raw_ip_value(value: str) -> bool:
    try:
        ipaddress.ip_address(value.strip())
    except ValueError:
        return False
    return True


def _sanitize_evidence(value: Any, *, key: str | None = None) -> Any:
    if key is not None and key.lower() in _RAW_IP_KEYS:
        return _REDACTED
    if isinstance(value, str):
        return _REDACTED if _is_raw_ip_value(value) else value
    if isinstance(value, Mapping):
        return {
            str(child_key): _sanitize_evidence(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_evidence(child) for child in value]
    if isinstance(value, tuple):
        return [_sanitize_evidence(child) for child in value]
    return value


def _principal_uuid(raw: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


async def _ranked_handles(
    session: AsyncSession,
    principal_ids: Sequence[str],
) -> dict[str, str]:
    parsed = {principal_id: _principal_uuid(principal_id) for principal_id in principal_ids}
    uuid_ids = [principal_id for principal_id in parsed.values() if principal_id is not None]
    stored: dict[uuid.UUID, str | None] = {}
    if uuid_ids:
        stmt = select(Principal.id, Principal.ranked_handle).where(Principal.id.in_(uuid_ids))
        for principal_id, ranked_handle in (await session.execute(stmt)).all():
            stored[principal_id] = ranked_handle

    handles: dict[str, str] = {}
    for raw, principal_id in parsed.items():
        if principal_id is None:
            handles[raw] = "unknown"
            continue
        handle = stored.get(principal_id)
        handles[raw] = handle.strip() if handle else derive_default_ranked_handle(principal_id)
    return handles


def _flag_response(row: RankedFlag, *, ranked_handle: str) -> AnticheatFlagResponse:
    evidence = _sanitize_evidence(row.evidence)
    if not isinstance(evidence, dict):  # pragma: no cover - model column is dict-shaped.
        evidence = {}
    return AnticheatFlagResponse(
        id=row.id,
        kind=row.kind,
        principal_id=row.principal_id,
        ranked_handle=ranked_handle,
        severity=row.severity,
        evidence=evidence,
        evidence_hash=row.evidence_hash,
        created_at=row.created_at,
    )


def _signal_response(
    row: RankedGameSignal,
    *,
    ranked_handle: str,
) -> RankedGameSignalResponse:
    signals = _sanitize_evidence(row.signals)
    if not isinstance(signals, dict):  # pragma: no cover - model column is dict-shaped.
        signals = {}
    return RankedGameSignalResponse(
        id=row.id,
        game_id=row.game_id,
        human_principal_id=row.human_principal_id,
        ranked_handle=ranked_handle,
        source_ip_hash=row.source_ip_hash,
        session_fingerprint_hash=row.session_fingerprint_hash,
        winner=row.winner,
        faction=row.faction,
        signals=signals,
        created_at=row.created_at,
    )


@router.get("/flags", response_model=CursorPage[AnticheatFlagResponse])
async def list_anticheat_flags(
    query: Annotated[AnticheatFlagListQuery, Query()],
    session: AsyncSession = Depends(get_session),
) -> CursorPage[AnticheatFlagResponse]:
    stmt = select(RankedFlag)
    if query.kind is not None:
        stmt = stmt.where(RankedFlag.kind == query.kind)
    if query.severity is not None:
        stmt = stmt.where(RankedFlag.severity == query.severity)
    if query.principal_id is not None:
        stmt = stmt.where(RankedFlag.principal_id == str(query.principal_id))

    rows, next_cursor = await paginate_keyset(
        session,
        stmt,
        created_at_col=RankedFlag.created_at,
        id_col=RankedFlag.id,
        limit=query.limit,
        cursor=query.cursor,
    )
    handles = await _ranked_handles(session, [row.principal_id for row in rows])
    return CursorPage[AnticheatFlagResponse](
        items=[_flag_response(row, ranked_handle=handles[row.principal_id]) for row in rows],
        next_cursor=next_cursor,
    )


@router.get("/principals/{principal_id}", response_model=PrincipalAnticheatRollup)
async def get_principal_anticheat_rollup(
    principal_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> PrincipalAnticheatRollup:
    raw_principal_id = str(principal_id)
    handles = await _ranked_handles(session, [raw_principal_id])
    flags = list(
        (
            await session.execute(
                select(RankedFlag)
                .where(RankedFlag.principal_id == raw_principal_id)
                .order_by(RankedFlag.created_at, RankedFlag.id)
            )
        ).scalars()
    )
    signals = list(
        (
            await session.execute(
                select(RankedGameSignal)
                .where(RankedGameSignal.human_principal_id == principal_id)
                .order_by(RankedGameSignal.created_at, RankedGameSignal.id)
            )
        ).scalars()
    )
    wins = sum(1 for row in signals if row.winner.upper() == row.faction.upper())
    return PrincipalAnticheatRollup(
        principal_id=principal_id,
        ranked_handle=handles[raw_principal_id],
        ranked_game_count=len({row.game_id for row in signals}),
        distinct_source_ip_hash_count=len({row.source_ip_hash for row in signals}),
        win_rate=(wins / len(signals)) if signals else None,
        flags=[_flag_response(row, ranked_handle=handles[raw_principal_id]) for row in flags],
    )


@router.get("/games/{game_id}/signals", response_model=RankedGameSignalsResponse)
async def get_game_anticheat_signals(
    game_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> RankedGameSignalsResponse:
    rows = list(
        (
            await session.execute(
                select(RankedGameSignal)
                .where(RankedGameSignal.game_id == game_id)
                .order_by(RankedGameSignal.created_at, RankedGameSignal.id)
            )
        ).scalars()
    )
    handles = await _ranked_handles(session, [str(row.human_principal_id) for row in rows])
    return RankedGameSignalsResponse(
        game_id=game_id,
        items=[
            _signal_response(row, ranked_handle=handles[str(row.human_principal_id)])
            for row in rows
        ],
    )


__all__ = [
    "AnticheatFlagResponse",
    "PrincipalAnticheatRollup",
    "RankedGameSignalResponse",
    "RankedGameSignalsResponse",
    "router",
]
