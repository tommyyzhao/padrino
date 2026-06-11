"""US-097: Curated roster + continuous matchmaker tests.

Covers: determinism, faction balance (seat rotation), roster-only selection,
least-played preference, error handling, and correct ruleset integration.
"""

from __future__ import annotations

import uuid

import pytest

from padrino.matchmaking.matchmaker import MatchRecord, next_match

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_ROSTER_7 = [uuid.uuid4() for _ in range(7)]
_ROSTER_10 = [uuid.uuid4() for _ in range(10)]
_SEED = "test-seed-us097"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_same_inputs_produce_same_plan() -> None:
    """Identical (roster, history, seed) always returns the same MatchPlan."""
    plan1 = next_match(_ROSTER_7, [], seed=_SEED)
    plan2 = next_match(_ROSTER_7, [], seed=_SEED)
    assert plan1.ruleset_id == plan2.ruleset_id
    assert plan1.gauntlet_seed == plan2.gauntlet_seed
    assert plan1.roster_by_seat == plan2.roster_by_seat


def test_different_seed_produces_different_assignment() -> None:
    """Different external seeds produce different seat assignments."""
    plan_a = next_match(_ROSTER_7, [], seed="seed-alpha")
    plan_b = next_match(_ROSTER_7, [], seed="seed-beta")
    assert plan_a.roster_by_seat != plan_b.roster_by_seat


def test_gauntlet_seed_differs_with_history_length() -> None:
    """Adding a history record changes the derived gauntlet seed."""
    plan0 = next_match(_ROSTER_7, [], seed=_SEED)
    record = MatchRecord(participants=tuple(plan0.roster_by_seat.values()))
    plan1 = next_match(_ROSTER_7, [record], seed=_SEED)
    assert plan0.gauntlet_seed != plan1.gauntlet_seed


# ---------------------------------------------------------------------------
# Faction balance (seat rotation)
# ---------------------------------------------------------------------------


def test_faction_balance_across_seeds() -> None:
    """Seat P01 is occupied by multiple different agents across distinct seeds."""
    roster = [uuid.uuid4() for _ in range(7)]
    p01_agents: set[uuid.UUID] = set()
    for i in range(20):
        plan = next_match(roster, [], seed=f"balance-seed-{i}")
        p01_agents.add(plan.roster_by_seat["P01"])
    # With 20 independent seeds, seat P01 should draw from multiple agents.
    assert len(p01_agents) > 1


def test_all_seats_occupied_no_duplicates() -> None:
    """Each game uses exactly player_count agents with no agent appearing twice."""
    plan = next_match(_ROSTER_7, [], seed=_SEED)
    seat_values = list(plan.roster_by_seat.values())
    assert len(seat_values) == 7
    assert len(set(seat_values)) == 7  # no duplicates


def test_seat_ids_are_canonical() -> None:
    """Seat keys are P01..P07 (mini7_v1 canonical form)."""
    plan = next_match(_ROSTER_7, [], seed=_SEED)
    expected = {f"P{i + 1:02d}" for i in range(7)}
    assert set(plan.roster_by_seat.keys()) == expected


# ---------------------------------------------------------------------------
# Roster-only selection
# ---------------------------------------------------------------------------


def test_all_selected_agents_come_from_roster() -> None:
    """Every agent in roster_by_seat must be an element of the input roster."""
    plan = next_match(_ROSTER_10, [], seed=_SEED)
    for agent_id in plan.roster_by_seat.values():
        assert agent_id in _ROSTER_10


def test_larger_roster_selects_exactly_player_count() -> None:
    """With a 10-agent roster, exactly 7 seats are assigned for mini7_v1."""
    plan = next_match(_ROSTER_10, [], seed=_SEED)
    assert len(plan.roster_by_seat) == 7


# ---------------------------------------------------------------------------
# Least-played preference
# ---------------------------------------------------------------------------


def test_fresh_agents_preferred_over_overplayed() -> None:
    """Agents with zero history appearances are preferred over heavily played ones."""
    overplayed = [uuid.uuid4() for _ in range(7)]
    fresh = [uuid.uuid4() for _ in range(3)]
    roster = overplayed + fresh  # 10 agents total
    # Simulate overplayed agents each having participated in 5 games.
    history = [MatchRecord(participants=tuple(overplayed)) for _ in range(5)]
    plan = next_match(roster, history, seed="prefer-fresh")
    selected = set(plan.roster_by_seat.values())
    for agent in fresh:
        assert agent in selected, "fresh agent should be selected before overplayed ones"


def test_history_participants_outside_roster_ignored() -> None:
    """MatchRecord participants not in the roster do not affect play counts."""
    outsider = uuid.uuid4()
    roster = [uuid.uuid4() for _ in range(7)]
    history = [MatchRecord(participants=(outsider,))]
    # Should not raise; outsider is silently skipped.
    plan = next_match(roster, history, seed=_SEED)
    assert set(plan.roster_by_seat.values()).issubset(set(roster))


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_raises_if_roster_too_small() -> None:
    """ValueError when roster has fewer agents than the ruleset player count."""
    small_roster = [uuid.uuid4() for _ in range(3)]
    with pytest.raises(ValueError, match="requires at least"):
        next_match(small_roster, [], seed=_SEED)


def test_raises_if_roster_empty() -> None:
    """ValueError when roster is empty."""
    with pytest.raises(ValueError, match="requires at least"):
        next_match([], [], seed=_SEED)


# ---------------------------------------------------------------------------
# Plan metadata
# ---------------------------------------------------------------------------


def test_plan_carries_correct_ruleset_id() -> None:
    plan = next_match(_ROSTER_7, [], seed=_SEED)
    assert plan.ruleset_id == "mini7_v1"


def test_plan_gauntlet_seed_is_hex_string() -> None:
    """gauntlet_seed is a 64-character hex digest (SHA-256)."""
    plan = next_match(_ROSTER_7, [], seed=_SEED)
    assert len(plan.gauntlet_seed) == 64
    assert all(c in "0123456789abcdef" for c in plan.gauntlet_seed)


def test_match_plan_equality_used_in_determinism_check() -> None:
    """MatchPlan instances with identical fields compare equal."""
    plan1 = next_match(_ROSTER_7, [], seed=_SEED)
    plan2 = next_match(_ROSTER_7, [], seed=_SEED)
    assert plan1 == plan2
