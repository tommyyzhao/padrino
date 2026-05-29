"""Unit tests for the pure-core seat-permutation helper (US-084)."""

from __future__ import annotations

import pytest

from padrino.core.rulesets import mini7_v1
from padrino.core.seating import seat_permutation


def test_is_a_permutation() -> None:
    perm = seat_permutation("gauntlet-seed:0", mini7_v1.PLAYER_COUNT)
    assert sorted(perm) == list(range(mini7_v1.PLAYER_COUNT))


def test_deterministic_in_seed() -> None:
    a = seat_permutation("seed:3", 7)
    b = seat_permutation("seed:3", 7)
    assert a == b


def test_different_seeds_usually_differ() -> None:
    perms = {seat_permutation(f"g:{i}", 7) for i in range(10)}
    # 10 independent seeds should not all collapse to the identity.
    assert len(perms) > 1


def test_rejects_nonpositive_player_count() -> None:
    with pytest.raises(ValueError, match="player_count must be > 0"):
        seat_permutation("seed", 0)


def test_seat_exposure_invariant_for_distinct_roster() -> None:
    """Across N games each of 7 distinct roster slots occupies exactly N seats.

    With ``len(roster) == PLAYER_COUNT == 7`` the AC bound
    ``floor(N*7/7) .. ceil(N*7/7)`` collapses to exactly N. Each slot must also
    land in more than one distinct seat index over the tournament — otherwise
    the permutation would not be rotating roles.
    """
    n_games = 10
    n = mini7_v1.PLAYER_COUNT
    seats_per_slot = dict.fromkeys(range(n), 0)
    positions_per_slot: dict[int, set[int]] = {slot: set() for slot in range(n)}
    for game_index in range(n_games):
        perm = seat_permutation(f"tournament-seed:{game_index}", n)
        for seat_index, slot in enumerate(perm):
            seats_per_slot[slot] += 1
            positions_per_slot[slot].add(seat_index)

    lower = (n_games * n) // n
    upper = -(-(n_games * n) // n)  # ceil
    assert lower == upper == n_games
    for slot in range(n):
        assert lower <= seats_per_slot[slot] <= upper, (
            f"slot {slot} occupied {seats_per_slot[slot]} seats; expected {n_games}"
        )
        assert len(positions_per_slot[slot]) >= 2, (
            f"slot {slot} never moved seats across {n_games} games"
        )
