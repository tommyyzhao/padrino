"""Pure lobby seat-claim planner tests (US-326)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from hypothesis import given
from hypothesis import strategies as st

from padrino.core.enums import LobbySeatKind
from padrino.core.lobby.seating import (
    AiReservationPolicy,
    LobbySeatPlanEntry,
    LobbySeatPlanError,
    LobbySeatPlanReason,
    claim_lowest_available_ai_seat,
    release_member_seat,
)


def _seat(
    seat_index: int,
    seat_kind: LobbySeatKind,
    *,
    member_id: str | None = None,
    agent_build_id: str | None = None,
    ai_reservation_policy: AiReservationPolicy = AiReservationPolicy.PRESERVE,
) -> LobbySeatPlanEntry:
    return LobbySeatPlanEntry(
        seat_index=seat_index,
        seat_kind=seat_kind,
        member_id=member_id,
        agent_build_id=agent_build_id,
        ai_reservation_policy=ai_reservation_policy,
    )


def _assert_reason(exc: pytest.ExceptionInfo[LobbySeatPlanError], reason: str) -> None:
    assert exc.value.reason == reason
    assert str(exc.value) == reason


def test_claim_assigns_lowest_index_eligible_ai_seat_deterministically() -> None:
    seats = (
        _seat(5, LobbySeatKind.AI, agent_build_id="build-5"),
        _seat(0, LobbySeatKind.HUMAN, member_id="host"),
        _seat(2, LobbySeatKind.AI, agent_build_id="build-2"),
    )

    first = claim_lowest_available_ai_seat(seats, joining_member_id="friend")
    second = claim_lowest_available_ai_seat(reversed(seats), joining_member_id="friend")

    assert first == second
    assert tuple(seat.seat_index for seat in first) == (0, 2, 5)
    claimed = first[1]
    assert claimed.seat_index == 2
    assert claimed.seat_kind == LobbySeatKind.HUMAN
    assert claimed.member_id == "friend"
    assert claimed.agent_build_id == "build-2"


def test_claim_returns_immutable_seat_plan() -> None:
    plan = claim_lowest_available_ai_seat(
        (
            _seat(0, LobbySeatKind.HUMAN, member_id="host"),
            _seat(1, LobbySeatKind.AI),
        ),
        joining_member_id="friend",
    )

    assert isinstance(plan, tuple)
    with pytest.raises(FrozenInstanceError):
        plan[1].member_id = "other"  # type: ignore[misc]


def test_claim_rejects_full_lobby() -> None:
    with pytest.raises(LobbySeatPlanError) as exc:
        claim_lowest_available_ai_seat(
            (
                _seat(0, LobbySeatKind.HUMAN, member_id="host"),
                _seat(1, LobbySeatKind.HUMAN, member_id="friend"),
            ),
            joining_member_id="late",
        )
    _assert_reason(exc, LobbySeatPlanReason.LOBBY_FULL)


def test_claim_rejects_member_already_seated() -> None:
    with pytest.raises(LobbySeatPlanError) as exc:
        claim_lowest_available_ai_seat(
            (
                _seat(0, LobbySeatKind.HUMAN, member_id="host"),
                _seat(1, LobbySeatKind.AI),
            ),
            joining_member_id="host",
        )
    _assert_reason(exc, LobbySeatPlanReason.DUPLICATE_MEMBER_SEATING)


def test_claim_rejects_duplicate_member_seating_in_layout() -> None:
    with pytest.raises(LobbySeatPlanError) as exc:
        claim_lowest_available_ai_seat(
            (
                _seat(0, LobbySeatKind.HUMAN, member_id="host"),
                _seat(1, LobbySeatKind.HUMAN, member_id="host"),
                _seat(2, LobbySeatKind.AI),
            ),
            joining_member_id="friend",
        )
    _assert_reason(exc, LobbySeatPlanReason.DUPLICATE_MEMBER_SEATING)


@pytest.mark.parametrize(
    "seats",
    [
        (),
        (
            _seat(0, LobbySeatKind.HUMAN, member_id="host"),
            _seat(0, LobbySeatKind.AI),
        ),
        (_seat(-1, LobbySeatKind.AI),),
        (
            _seat(0, LobbySeatKind.HUMAN, member_id="host"),
            _seat(1, LobbySeatKind.AI, member_id="friend"),
        ),
        (
            _seat(0, LobbySeatKind.HUMAN, member_id="host"),
            LobbySeatPlanEntry(seat_index=1, seat_kind="BOT"),
        ),
    ],
)
def test_claim_rejects_malformed_layouts(seats: tuple[LobbySeatPlanEntry, ...]) -> None:
    with pytest.raises(LobbySeatPlanError) as exc:
        claim_lowest_available_ai_seat(seats, joining_member_id="friend")
    _assert_reason(exc, LobbySeatPlanReason.MALFORMED_LAYOUT)


def test_canonicalizes_string_kinds_from_persisted_rows() -> None:
    plan = claim_lowest_available_ai_seat(
        (
            LobbySeatPlanEntry(seat_index=0, seat_kind="HUMAN", member_id="host"),
            LobbySeatPlanEntry(seat_index=1, seat_kind="AI", agent_build_id="build-1"),
        ),
        joining_member_id="friend",
    )

    assert plan == (
        _seat(0, LobbySeatKind.HUMAN, member_id="host"),
        _seat(1, LobbySeatKind.HUMAN, member_id="friend", agent_build_id="build-1"),
    )


def test_claim_rejects_vacant_reserved_human_seat() -> None:
    with pytest.raises(LobbySeatPlanError) as exc:
        claim_lowest_available_ai_seat(
            (
                _seat(0, LobbySeatKind.HUMAN, member_id="host"),
                _seat(1, LobbySeatKind.HUMAN),
            ),
            joining_member_id="friend",
        )
    _assert_reason(exc, LobbySeatPlanReason.RESERVED_HUMAN_SEAT)


def test_release_clears_only_departing_members_seat_and_preserves_ai_reservation() -> None:
    occupied = claim_lowest_available_ai_seat(
        (
            _seat(0, LobbySeatKind.HUMAN, member_id="host"),
            _seat(1, LobbySeatKind.AI, agent_build_id="build-1"),
            _seat(2, LobbySeatKind.AI, agent_build_id="build-2"),
        ),
        joining_member_id="friend",
    )

    released = release_member_seat(occupied, departing_member_id="friend")

    assert released == (
        _seat(0, LobbySeatKind.HUMAN, member_id="host"),
        _seat(1, LobbySeatKind.AI, agent_build_id="build-1"),
        _seat(2, LobbySeatKind.AI, agent_build_id="build-2"),
    )


def test_release_can_clear_ai_reservation_when_policy_says_clear() -> None:
    occupied = claim_lowest_available_ai_seat(
        (
            _seat(0, LobbySeatKind.HUMAN, member_id="host"),
            _seat(
                1,
                LobbySeatKind.AI,
                agent_build_id="build-1",
                ai_reservation_policy=AiReservationPolicy.CLEAR,
            ),
        ),
        joining_member_id="friend",
    )

    released = release_member_seat(occupied, departing_member_id="friend")

    assert released == (
        _seat(0, LobbySeatKind.HUMAN, member_id="host"),
        _seat(1, LobbySeatKind.AI, ai_reservation_policy=AiReservationPolicy.CLEAR),
    )


def test_release_rejects_absent_member() -> None:
    with pytest.raises(LobbySeatPlanError) as exc:
        release_member_seat(
            (
                _seat(0, LobbySeatKind.HUMAN, member_id="host"),
                _seat(1, LobbySeatKind.AI),
            ),
            departing_member_id="friend",
        )
    _assert_reason(exc, LobbySeatPlanReason.MEMBER_NOT_SEATED)


def _layout(existing_humans: int, total_seats: int) -> tuple[LobbySeatPlanEntry, ...]:
    return tuple(
        _seat(index, LobbySeatKind.HUMAN, member_id=f"member-{index}")
        if index < existing_humans
        else _seat(index, LobbySeatKind.AI, agent_build_id=f"build-{index}")
        for index in range(total_seats)
    )


@given(total_seats=st.integers(min_value=2, max_value=15), existing_humans=st.data())
def test_join_properties_and_release_round_trip(
    total_seats: int, existing_humans: st.DataObject
) -> None:
    human_count = existing_humans.draw(st.integers(min_value=1, max_value=total_seats - 1))
    original = _layout(human_count, total_seats)

    joined = claim_lowest_available_ai_seat(original, joining_member_id="joining-member")
    member_ids = [seat.member_id for seat in joined if seat.member_id is not None]

    assert member_ids.count("joining-member") == 1
    assert len(member_ids) == len(set(member_ids))
    assert len({seat.seat_index for seat in joined}) == total_seats
    assert sum(seat.seat_kind == LobbySeatKind.HUMAN for seat in joined) == len(member_ids)
    assert joined[human_count].member_id == "joining-member"

    released = release_member_seat(joined, departing_member_id="joining-member")
    assert released == original
