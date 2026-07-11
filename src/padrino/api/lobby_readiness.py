"""Impure-shell adapters for pure lobby readiness facts (US-329)."""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Protocol

from padrino.api.lobby_presence import is_present
from padrino.core.enums import LobbySeatKind
from padrino.core.lobby.readiness import (
    LobbyReadinessDecision,
    LobbyReadinessMember,
    LobbyReadinessSeat,
    evaluate_lobby_readiness,
)


class _MemberRow(Protocol):
    id: uuid.UUID
    is_host: bool
    ready: bool
    joined_at: datetime
    last_seen_at: datetime | None


class _SeatRow(Protocol):
    seat_index: int
    seat_kind: str
    member_id: uuid.UUID | None
    agent_build_id: uuid.UUID | None


def evaluate_lobby_rows(
    *,
    members: Iterable[_MemberRow],
    seats: Iterable[_SeatRow],
    expected_player_count: int,
    available_ai_build_count: int | None,
    now: datetime | None,
    stale_seconds: float | None,
) -> LobbyReadinessDecision:
    """Evaluate persisted lobby rows through the pure readiness contract."""
    return evaluate_lobby_readiness(
        members=tuple(
            LobbyReadinessMember(
                member_id=str(member.id),
                ready=member.ready,
                present=_present(member, now=now, stale_seconds=stale_seconds),
            )
            for member in members
        ),
        seats=tuple(
            LobbyReadinessSeat(
                seat_index=seat.seat_index,
                seat_kind=seat.seat_kind,
                member_id=str(seat.member_id) if seat.member_id is not None else None,
                agent_build_id=(
                    str(seat.agent_build_id) if seat.agent_build_id is not None else None
                ),
            )
            for seat in seats
        ),
        expected_player_count=expected_player_count,
        available_ai_build_count=available_ai_build_count,
    )


def available_auto_fill_build_count(
    *, seats: Iterable[_SeatRow], curated_roster: Sequence[str]
) -> int:
    """Return distinct curated builds available after excluding reserved AI seats."""
    reserved = {
        str(seat.agent_build_id)
        for seat in seats
        if seat.seat_kind == LobbySeatKind.AI.value and seat.agent_build_id is not None
    }
    seen: set[str] = set()
    available = 0
    for build_id in curated_roster:
        if build_id in seen or build_id in reserved:
            continue
        seen.add(build_id)
        available += 1
    return available


def _present(member: _MemberRow, *, now: datetime | None, stale_seconds: float | None) -> bool:
    if now is None or stale_seconds is None:
        return True
    return is_present(member, now=now, stale_seconds=stale_seconds)


__all__ = [
    "available_auto_fill_build_count",
    "evaluate_lobby_rows",
]
