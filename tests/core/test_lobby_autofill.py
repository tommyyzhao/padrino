"""Tests for the pure curated auto-fill of empty lobby seats (US-149).

Auto-fill must be PURE and DETERMINISTIC: the same ``lobby_seed`` + reserved map
+ curated roster always yields the same seat -> build assignment (so a launch is
reproducible and replay-stable). It uses only ``SeededRng`` — no ``random``, no
clock, no IO (enforced by ``tests/core/test_purity.py``). It raises when the
curated pool is smaller than the number of empty seats.
"""

from __future__ import annotations

import pytest

from padrino.core.lobby.autofill import (
    NotEnoughCuratedModelsError,
    autofill_empty_seats,
)

_ROSTER = [f"build-{i:02d}" for i in range(8)]


def test_assigns_every_empty_seat_a_distinct_build() -> None:
    result = autofill_empty_seats(
        lobby_seed="seed-abc",
        empty_seat_indices=[1, 2, 3, 4, 5, 6],
        reserved_build_ids={},
        curated_roster=_ROSTER,
    )
    assert set(result.keys()) == {1, 2, 3, 4, 5, 6}
    # Distinct seats get distinct builds (no duplicate model in one game).
    assert len(set(result.values())) == len(result)
    assert set(result.values()) <= set(_ROSTER)


def test_is_deterministic_in_seed_and_inputs() -> None:
    kwargs = {
        "lobby_seed": "seed-xyz",
        "empty_seat_indices": [2, 4, 6],
        "reserved_build_ids": {1: "build-00"},
        "curated_roster": _ROSTER,
    }
    first = autofill_empty_seats(**kwargs)  # type: ignore[arg-type]
    second = autofill_empty_seats(**kwargs)  # type: ignore[arg-type]
    assert first == second


def test_seat_order_does_not_change_assignment() -> None:
    a = autofill_empty_seats(
        lobby_seed="seed-1",
        empty_seat_indices=[1, 2, 3],
        reserved_build_ids={},
        curated_roster=_ROSTER,
    )
    b = autofill_empty_seats(
        lobby_seed="seed-1",
        empty_seat_indices=[3, 1, 2],
        reserved_build_ids={},
        curated_roster=_ROSTER,
    )
    assert a == b


def test_different_seed_can_change_assignment() -> None:
    a = autofill_empty_seats(
        lobby_seed="seed-1",
        empty_seat_indices=[1, 2, 3, 4, 5, 6],
        reserved_build_ids={},
        curated_roster=_ROSTER,
    )
    b = autofill_empty_seats(
        lobby_seed="seed-2",
        empty_seat_indices=[1, 2, 3, 4, 5, 6],
        reserved_build_ids={},
        curated_roster=_ROSTER,
    )
    # Same keys, but the seed should permute the model placement.
    assert a.keys() == b.keys()
    assert a != b


def test_excludes_already_reserved_builds() -> None:
    reserved = {1: "build-00", 2: "build-01"}
    result = autofill_empty_seats(
        lobby_seed="seed-r",
        empty_seat_indices=[3, 4, 5, 6],
        reserved_build_ids=reserved,
        curated_roster=_ROSTER,
    )
    assert "build-00" not in result.values()
    assert "build-01" not in result.values()
    # No model collides with a reserved seat's model.
    assert not (set(result.values()) & set(reserved.values()))


def test_raises_when_pool_smaller_than_empty_seats() -> None:
    with pytest.raises(NotEnoughCuratedModelsError) as exc:
        autofill_empty_seats(
            lobby_seed="seed-small",
            empty_seat_indices=[1, 2, 3],
            reserved_build_ids={},
            curated_roster=["build-00", "build-01"],
        )
    assert exc.value.empty_seats == 3
    assert exc.value.pool_size == 2


def test_raises_when_reserved_consumes_the_pool() -> None:
    # Two builds in the pool, but one is reserved -> only one available for two seats.
    with pytest.raises(NotEnoughCuratedModelsError):
        autofill_empty_seats(
            lobby_seed="seed-consumed",
            empty_seat_indices=[2, 3],
            reserved_build_ids={1: "build-00"},
            curated_roster=["build-00", "build-01"],
        )


def test_no_empty_seats_returns_empty_mapping() -> None:
    result = autofill_empty_seats(
        lobby_seed="seed-none",
        empty_seat_indices=[],
        reserved_build_ids={1: "build-00"},
        curated_roster=_ROSTER,
    )
    assert result == {}


def test_duplicate_roster_entries_count_once() -> None:
    # A roster of 2 distinct ids (repeated) cannot fill 3 seats.
    with pytest.raises(NotEnoughCuratedModelsError):
        autofill_empty_seats(
            lobby_seed="seed-dup",
            empty_seat_indices=[1, 2, 3],
            reserved_build_ids={},
            curated_roster=["build-00", "build-01", "build-00", "build-01"],
        )
