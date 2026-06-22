"""Tests for deterministic role assignment from a game seed."""

from __future__ import annotations

from collections import Counter

from padrino.core.engine.role_assignment import assign_roles
from padrino.core.engine.state import Seat
from padrino.core.enums import Faction, Role
from padrino.core.rulesets import (
    bench10_v1,
    deception13_v1,
    jester8_v1,
    mini7_v1,
    roleblock10_v1,
    visit12_v1,
)


def test_returns_seven_seats() -> None:
    seats = assign_roles("seed-1", mini7_v1)
    assert len(seats) == mini7_v1.PLAYER_COUNT == 7


def test_same_seed_same_assignment() -> None:
    a = assign_roles("identical-seed", mini7_v1)
    b = assign_roles("identical-seed", mini7_v1)
    assert a == b


def test_different_seeds_usually_differ() -> None:
    # Across 100 distinct seeds, at least two adjacent assignments should differ.
    distinct = {tuple(s.role for s in assign_roles(f"seed-{i}", mini7_v1)) for i in range(100)}
    assert len(distinct) > 1


def test_role_counts_exact() -> None:
    for i in range(50):
        seats = assign_roles(f"trial-{i}", mini7_v1)
        counts = Counter(s.role for s in seats)
        assert counts[Role.MAFIA_GOON] == 2
        assert counts[Role.GODFATHER] == 0
        assert counts[Role.DETECTIVE] == 1
        assert counts[Role.DOCTOR] == 1
        assert counts[Role.VILLAGER] == 3


def test_bench10_role_counts_include_one_godfather() -> None:
    for i in range(50):
        seats = assign_roles(f"bench-trial-{i}", bench10_v1)
        counts = Counter(s.role for s in seats)
        assert counts[Role.MAFIA_GOON] == 2
        assert counts[Role.GODFATHER] == 1
        assert counts[Role.DETECTIVE] == 1
        assert counts[Role.DOCTOR] == 1
        assert counts[Role.VILLAGER] == 5


def test_roleblock10_role_counts_include_one_mafia_roleblocker() -> None:
    for i in range(50):
        seats = assign_roles(f"roleblock-trial-{i}", roleblock10_v1)
        counts = Counter(s.role for s in seats)
        assert counts[Role.MAFIA_GOON] == 2
        assert counts[Role.MAFIA_ROLEBLOCKER] == 1
        assert counts[Role.DETECTIVE] == 1
        assert counts[Role.DOCTOR] == 1
        assert counts[Role.VILLAGER] == 5


def test_deception13_role_counts_include_vetted_scum_skills() -> None:
    for i in range(50):
        seats = assign_roles(f"deception-trial-{i}", deception13_v1)
        counts = Counter(s.role for s in seats)
        assert counts[Role.GODFATHER] == 1
        assert counts[Role.MAFIA_ROLEBLOCKER] == 1
        assert counts[Role.JANITOR] == 1
        assert counts[Role.MAFIA_GOON] == 1
        assert counts[Role.FRAMER] == 0
        assert counts[Role.DETECTIVE] == 1
        assert counts[Role.DOCTOR] == 1
        assert counts[Role.VILLAGER] == 7


def test_visit12_role_counts_include_tracker_and_watcher() -> None:
    for i in range(50):
        seats = assign_roles(f"visit-trial-{i}", visit12_v1)
        counts = Counter(s.role for s in seats)
        assert counts[Role.MAFIA_GOON] == 2
        assert counts[Role.MAFIA_ROLEBLOCKER] == 1
        assert counts[Role.DETECTIVE] == 1
        assert counts[Role.DOCTOR] == 1
        assert counts[Role.TRACKER] == 1
        assert counts[Role.WATCHER] == 1
        assert counts[Role.VILLAGER] == 5


def test_jester8_role_counts_include_one_jester() -> None:
    for i in range(50):
        seats = assign_roles(f"jester-trial-{i}", jester8_v1)
        counts = Counter(s.role for s in seats)
        assert counts[Role.MAFIA_GOON] == 2
        assert counts[Role.JESTER] == 1
        assert counts[Role.DETECTIVE] == 1
        assert counts[Role.DOCTOR] == 1
        assert counts[Role.VILLAGER] == 3
        jester = next(seat for seat in seats if seat.role is Role.JESTER)
        assert jester.faction is Faction.JESTER


def test_public_player_ids_are_p01_through_p07() -> None:
    seats = assign_roles("ids", mini7_v1)
    ids = [s.public_player_id for s in seats]
    assert ids == [f"P0{i}" for i in range(1, 8)]


def test_seat_indices_are_zero_through_six() -> None:
    seats = assign_roles("indices", mini7_v1)
    assert [s.seat_index for s in seats] == list(range(7))


def test_public_player_ids_unique() -> None:
    seats = assign_roles("uniq", mini7_v1)
    assert len({s.public_player_id for s in seats}) == 7


def test_all_seats_alive_initially() -> None:
    seats = assign_roles("alive", mini7_v1)
    assert all(s.alive for s in seats)
    assert all(s.death_phase is None for s in seats)
    assert all(s.last_protected_target is None for s in seats)
    assert all(s.queued_inspection_result is None for s in seats)


def test_factions_match_roles() -> None:
    seats = assign_roles("factions", mini7_v1)
    for seat in seats:
        if seat.role in {Role.MAFIA_GOON, Role.GODFATHER, Role.MAFIA_ROLEBLOCKER}:
            assert seat.faction == Faction.MAFIA
        else:
            assert seat.faction == Faction.TOWN


def test_returns_list_of_seats() -> None:
    seats = assign_roles("type", mini7_v1)
    assert isinstance(seats, list)
    assert all(isinstance(s, Seat) for s in seats)


def test_role_distribution_across_seeds_is_not_constant() -> None:
    # The position of the mafia seats must depend on the seed.
    first_mafia_positions = set()
    for i in range(200):
        seats = assign_roles(f"distribution-{i}", mini7_v1)
        mafia_seats = tuple(s.seat_index for s in seats if s.role == Role.MAFIA_GOON)
        first_mafia_positions.add(mafia_seats)
    assert len(first_mafia_positions) > 1
