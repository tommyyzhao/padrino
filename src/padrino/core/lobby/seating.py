"""Pure deterministic lobby seat-claim planner (US-326)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from padrino.core.enums import LobbySeatKind


class AiReservationPolicy(StrEnum):
    """How a released human seat restores any host-configured AI model."""

    PRESERVE = "PRESERVE"
    CLEAR = "CLEAR"


class LobbySeatPlanReason(StrEnum):
    """Stable machine tokens for rejected lobby seat transitions."""

    LOBBY_FULL = "lobby_full"
    DUPLICATE_MEMBER_SEATING = "duplicate_member_seating"
    MALFORMED_LAYOUT = "malformed_layout"
    RESERVED_HUMAN_SEAT = "reserved_human_seat"
    MEMBER_NOT_SEATED = "member_not_seated"


class LobbySeatPlanError(ValueError):
    """Raised when a pure lobby seating transition is not valid."""

    def __init__(self, reason: LobbySeatPlanReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True, slots=True)
class LobbySeatPlanEntry:
    """One immutable seat in a lobby's pre-launch seating plan."""

    seat_index: int
    seat_kind: LobbySeatKind | str
    member_id: str | None = None
    agent_build_id: str | None = None
    ai_reservation_policy: AiReservationPolicy | str = AiReservationPolicy.PRESERVE


LobbySeatPlan = tuple[LobbySeatPlanEntry, ...]


def claim_lowest_available_ai_seat(
    seats: Iterable[LobbySeatPlanEntry],
    *,
    joining_member_id: str,
    human_seat_capacity: int | None = None,
) -> LobbySeatPlan:
    """Return a new plan with ``joining_member_id`` on the lowest AI seat.

    The transition is data-in/data-out: no DB, clock, network, or randomness.
    Identical input yields identical canonical output sorted by ``seat_index``.
    """
    plan = _validate_and_canonicalize(seats)
    if any(seat.member_id == joining_member_id for seat in plan):
        raise LobbySeatPlanError(LobbySeatPlanReason.DUPLICATE_MEMBER_SEATING)
    if human_seat_capacity is not None and _human_count(plan) >= human_seat_capacity:
        raise LobbySeatPlanError(LobbySeatPlanReason.LOBBY_FULL)

    for seat in plan:
        if seat.seat_kind == LobbySeatKind.AI and _claimable_by_capacity(
            seat, human_seat_capacity=human_seat_capacity
        ):
            return tuple(
                _claim_seat(seat, joining_member_id=joining_member_id)
                if candidate.seat_index == seat.seat_index
                else candidate
                for candidate in plan
            )

    raise LobbySeatPlanError(LobbySeatPlanReason.LOBBY_FULL)


def release_member_seat(
    seats: Iterable[LobbySeatPlanEntry],
    *,
    departing_member_id: str,
) -> LobbySeatPlan:
    """Return a new plan with exactly ``departing_member_id`` released to AI."""
    plan = _validate_and_canonicalize(seats)
    matches = [seat for seat in plan if seat.member_id == departing_member_id]
    if not matches:
        raise LobbySeatPlanError(LobbySeatPlanReason.MEMBER_NOT_SEATED)
    if len(matches) > 1:
        raise LobbySeatPlanError(LobbySeatPlanReason.DUPLICATE_MEMBER_SEATING)

    departing_seat = matches[0]
    return tuple(
        _release_seat(seat) if seat.seat_index == departing_seat.seat_index else seat
        for seat in plan
    )


def _claim_seat(seat: LobbySeatPlanEntry, *, joining_member_id: str) -> LobbySeatPlanEntry:
    return LobbySeatPlanEntry(
        seat_index=seat.seat_index,
        seat_kind=LobbySeatKind.HUMAN,
        member_id=joining_member_id,
        agent_build_id=(
            seat.agent_build_id
            if seat.ai_reservation_policy == AiReservationPolicy.PRESERVE
            else None
        ),
        ai_reservation_policy=seat.ai_reservation_policy,
    )


def _release_seat(seat: LobbySeatPlanEntry) -> LobbySeatPlanEntry:
    return LobbySeatPlanEntry(
        seat_index=seat.seat_index,
        seat_kind=LobbySeatKind.AI,
        member_id=None,
        agent_build_id=(
            seat.agent_build_id
            if seat.ai_reservation_policy == AiReservationPolicy.PRESERVE
            else None
        ),
        ai_reservation_policy=seat.ai_reservation_policy,
    )


def _human_count(seats: LobbySeatPlan) -> int:
    return sum(1 for seat in seats if seat.seat_kind == LobbySeatKind.HUMAN)


def _claimable_by_capacity(seat: LobbySeatPlanEntry, *, human_seat_capacity: int | None) -> bool:
    return human_seat_capacity is None or seat.seat_index < human_seat_capacity


def _validate_and_canonicalize(seats: Iterable[LobbySeatPlanEntry]) -> LobbySeatPlan:
    raw = tuple(seats)
    if not raw:
        raise LobbySeatPlanError(LobbySeatPlanReason.MALFORMED_LAYOUT)

    canonical = tuple(
        sorted((_normalize_seat(seat) for seat in raw), key=lambda seat: seat.seat_index)
    )
    _validate_indices(canonical)
    _validate_occupancy(canonical)
    return canonical


def _normalize_seat(seat: LobbySeatPlanEntry) -> LobbySeatPlanEntry:
    return LobbySeatPlanEntry(
        seat_index=seat.seat_index,
        seat_kind=_coerce_seat_kind(seat.seat_kind),
        member_id=seat.member_id,
        agent_build_id=seat.agent_build_id,
        ai_reservation_policy=_coerce_ai_reservation_policy(seat.ai_reservation_policy),
    )


def _coerce_seat_kind(value: LobbySeatKind | str) -> LobbySeatKind:
    try:
        return LobbySeatKind(value)
    except ValueError as exc:
        raise LobbySeatPlanError(LobbySeatPlanReason.MALFORMED_LAYOUT) from exc


def _coerce_ai_reservation_policy(value: AiReservationPolicy | str) -> AiReservationPolicy:
    try:
        return AiReservationPolicy(value)
    except ValueError as exc:
        raise LobbySeatPlanError(LobbySeatPlanReason.MALFORMED_LAYOUT) from exc


def _validate_indices(seats: LobbySeatPlan) -> None:
    seen: set[int] = set()
    for seat in seats:
        if seat.seat_index < 0 or seat.seat_index in seen:
            raise LobbySeatPlanError(LobbySeatPlanReason.MALFORMED_LAYOUT)
        seen.add(seat.seat_index)


def _validate_occupancy(seats: LobbySeatPlan) -> None:
    member_ids: set[str] = set()
    for seat in seats:
        if seat.seat_kind not in {LobbySeatKind.AI, LobbySeatKind.HUMAN}:
            raise LobbySeatPlanError(LobbySeatPlanReason.MALFORMED_LAYOUT)
        if seat.seat_kind == LobbySeatKind.AI and seat.member_id is not None:
            raise LobbySeatPlanError(LobbySeatPlanReason.MALFORMED_LAYOUT)
        if seat.seat_kind == LobbySeatKind.HUMAN and seat.member_id is None:
            raise LobbySeatPlanError(LobbySeatPlanReason.RESERVED_HUMAN_SEAT)
        if seat.member_id is None:
            continue
        if seat.member_id in member_ids:
            raise LobbySeatPlanError(LobbySeatPlanReason.DUPLICATE_MEMBER_SEATING)
        member_ids.add(seat.member_id)


__all__ = [
    "AiReservationPolicy",
    "LobbySeatPlan",
    "LobbySeatPlanEntry",
    "LobbySeatPlanError",
    "LobbySeatPlanReason",
    "claim_lowest_available_ai_seat",
    "release_member_seat",
]
