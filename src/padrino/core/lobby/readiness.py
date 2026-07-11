"""Pure lobby launch-readiness evaluator (US-329)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from padrino.core.enums import LobbySeatKind


class LobbyReadinessReason(StrEnum):
    """Stable machine tokens for fail-closed lobby readiness decisions."""

    MISSING_SEAT = "missing_seat"
    DUPLICATE_SEAT = "duplicate_seat"
    ORPHAN_SEAT = "orphan_seat"
    ABSENT_MEMBER = "absent_member"
    NOT_READY_MEMBER = "not_ready_member"
    UNRESOLVED_AI_SEAT = "unresolved_ai_seat"
    COUNT_MISMATCH = "count_mismatch"


_REASON_ORDER: tuple[LobbyReadinessReason, ...] = tuple(LobbyReadinessReason)


@dataclass(frozen=True, slots=True)
class LobbyReadinessMember:
    """Pure readiness facts for one active lobby member."""

    member_id: str
    ready: bool
    present: bool


@dataclass(frozen=True, slots=True)
class LobbyReadinessSeat:
    """Pure readiness facts for one configured lobby seat."""

    seat_index: int
    seat_kind: LobbySeatKind | str
    member_id: str | None = None
    agent_build_id: str | None = None


@dataclass(frozen=True, slots=True)
class LobbyReadinessDecision:
    """Readiness outcome with reasons sorted by the public contract order."""

    allowed: bool
    reasons: tuple[LobbyReadinessReason, ...]


def evaluate_lobby_readiness(
    *,
    members: Iterable[LobbyReadinessMember],
    seats: Iterable[LobbyReadinessSeat],
    expected_player_count: int,
    available_ai_build_count: int | None,
) -> LobbyReadinessDecision:
    """Return whether a lobby can advance toward launch.

    ``available_ai_build_count`` is an injected impure-shell fact. ``None`` means
    the caller is not checking auto-fill capacity at this stage; an integer means
    the evaluator must prove all unresolved AI seats can be filled.
    """
    member_rows = tuple(members)
    seat_rows = tuple(seats)
    reasons: set[LobbyReadinessReason] = set()

    member_counts: dict[str, int] = {}
    for member in member_rows:
        member_counts[member.member_id] = member_counts.get(member.member_id, 0) + 1
        if not member.present:
            reasons.add(LobbyReadinessReason.ABSENT_MEMBER)
        if not member.ready:
            reasons.add(LobbyReadinessReason.NOT_READY_MEMBER)
    member_ids = set(member_counts)
    if any(count > 1 for count in member_counts.values()):
        reasons.add(LobbyReadinessReason.DUPLICATE_SEAT)

    if expected_player_count <= 0 or len(seat_rows) != expected_player_count:
        reasons.add(LobbyReadinessReason.COUNT_MISMATCH)

    seen_indices: set[int] = set()
    human_member_counts: dict[str, int] = {}
    unresolved_ai_seats = 0
    invalid_index = False

    for seat in seat_rows:
        if seat.seat_index < 0 or seat.seat_index >= expected_player_count:
            invalid_index = True
        if seat.seat_index in seen_indices:
            reasons.add(LobbyReadinessReason.DUPLICATE_SEAT)
        seen_indices.add(seat.seat_index)

        seat_kind = _coerce_seat_kind(seat.seat_kind)
        if seat_kind == LobbySeatKind.HUMAN:
            if seat.member_id is None or seat.member_id not in member_ids:
                reasons.add(LobbyReadinessReason.ORPHAN_SEAT)
            else:
                human_member_counts[seat.member_id] = human_member_counts.get(seat.member_id, 0) + 1
        elif seat_kind == LobbySeatKind.AI:
            if seat.member_id is not None:
                reasons.add(LobbyReadinessReason.ORPHAN_SEAT)
            if seat.agent_build_id is None:
                unresolved_ai_seats += 1
        else:
            reasons.add(LobbyReadinessReason.COUNT_MISMATCH)

    if seen_indices != set(range(expected_player_count)) or invalid_index:
        reasons.add(LobbyReadinessReason.COUNT_MISMATCH)

    if any(count > 1 for count in human_member_counts.values()):
        reasons.add(LobbyReadinessReason.DUPLICATE_SEAT)

    for member_id in member_ids:
        if human_member_counts.get(member_id, 0) != 1:
            reasons.add(LobbyReadinessReason.MISSING_SEAT)

    if available_ai_build_count is not None and unresolved_ai_seats > max(
        available_ai_build_count, 0
    ):
        reasons.add(LobbyReadinessReason.UNRESOLVED_AI_SEAT)

    ordered = tuple(reason for reason in _REASON_ORDER if reason in reasons)
    return LobbyReadinessDecision(allowed=not ordered, reasons=ordered)


def _coerce_seat_kind(value: LobbySeatKind | str) -> LobbySeatKind | None:
    try:
        return LobbySeatKind(value)
    except ValueError:
        return None


__all__ = [
    "LobbyReadinessDecision",
    "LobbyReadinessMember",
    "LobbyReadinessReason",
    "LobbyReadinessSeat",
    "evaluate_lobby_readiness",
]
