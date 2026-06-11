"""Tests for deterministic analytics core (US-102).

Uses an in-memory fixture game covering the three analytics dimensions:
  - per-role win rates
  - voting accuracy (votes that hit actual mafia)
  - survival curves by role
"""

from __future__ import annotations

from typing import Any

import pytest

from padrino.analytics.deterministic import (
    GameAnalytics,
    RoleWinRate,
    SurvivalPoint,
    VotingAccuracy,
    _compute_role_win_rates,
    _compute_survival_curve,
    _compute_voting_accuracy,
    _extract_role_map,
    _extract_winner,
    _phase_to_day,
    compute_game_analytics,
)

# ---------------------------------------------------------------------------
# Fixture game
# ---------------------------------------------------------------------------
# mini7_v1: P01+P02=MAFIA_GOON/MAFIA, P03=DETECTIVE/TOWN, P04=DOCTOR/TOWN,
#           P05+P06+P07=VILLAGER/TOWN
#
# Flow:
#   DAY_1_VOTE  : town (5 seats) votes P01 out. P01 eliminated (MAFIA).
#   NIGHT_1_ACTIONS: mafia kills P07 (VILLAGER). P07 eliminated.
#   DAY_2_VOTE  : town (4 seats) votes P02 out. P02 eliminated (MAFIA). TOWN wins.

_ROLE_ASSIGNMENTS: list[dict[str, str]] = [
    {"public_player_id": "P01", "role": "MAFIA_GOON", "faction": "MAFIA"},
    {"public_player_id": "P02", "role": "MAFIA_GOON", "faction": "MAFIA"},
    {"public_player_id": "P03", "role": "DETECTIVE", "faction": "TOWN"},
    {"public_player_id": "P04", "role": "DOCTOR", "faction": "TOWN"},
    {"public_player_id": "P05", "role": "VILLAGER", "faction": "TOWN"},
    {"public_player_id": "P06", "role": "VILLAGER", "faction": "TOWN"},
    {"public_player_id": "P07", "role": "VILLAGER", "faction": "TOWN"},
]


def _ev(
    seq: int,
    event_type: str,
    phase: str,
    visibility: str,
    actor: str | None,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "sequence": seq,
        "event_type": event_type,
        "phase": phase,
        "visibility": visibility,
        "actor_player_id": actor,
        "payload": payload,
        "prev_event_hash": "a" * 64,
        "event_hash": "b" * 64,
    }


FIXTURE_GAME: list[dict[str, Any]] = [
    _ev(
        1,
        "GameCreated",
        "SETUP",
        "SYSTEM",
        None,
        {"ruleset_id": "mini7_v1", "game_id": "game-test", "game_seed": "seed1", "player_count": 7},
    ),
    _ev(2, "RolesAssigned", "SETUP", "SYSTEM", None, {"assignments": _ROLE_ASSIGNMENTS}),
    _ev(
        3,
        "PhaseStarted",
        "DAY_1_VOTE",
        "SYSTEM",
        None,
        {"phase_kind": "DAY_VOTE", "day": 1, "round": 0},
    ),
    # Day 1 votes: town votes P01 (MAFIA); P01+P02 vote P03 (TOWN, inaccurate)
    _ev(4, "VoteSubmitted", "DAY_1_VOTE", "PUBLIC", "P03", {"target": "P01", "is_abstain": False}),
    _ev(5, "VoteSubmitted", "DAY_1_VOTE", "PUBLIC", "P04", {"target": "P01", "is_abstain": False}),
    _ev(6, "VoteSubmitted", "DAY_1_VOTE", "PUBLIC", "P05", {"target": "P01", "is_abstain": False}),
    _ev(7, "VoteSubmitted", "DAY_1_VOTE", "PUBLIC", "P06", {"target": "P01", "is_abstain": False}),
    _ev(8, "VoteSubmitted", "DAY_1_VOTE", "PUBLIC", "P07", {"target": "P01", "is_abstain": False}),
    _ev(9, "VoteSubmitted", "DAY_1_VOTE", "PUBLIC", "P01", {"target": "P03", "is_abstain": False}),
    _ev(10, "VoteSubmitted", "DAY_1_VOTE", "PUBLIC", "P02", {"target": "P03", "is_abstain": False}),
    _ev(
        11,
        "DayVoteResolved",
        "DAY_1_VOTE",
        "PUBLIC",
        None,
        {"eliminated": "P01", "vote_tally": {"P01": 5, "P03": 2}, "reason": "majority"},
    ),
    _ev(
        12,
        "PlayerEliminated",
        "DAY_1_VOTE",
        "PUBLIC",
        None,
        {"public_player_id": "P01", "role": "MAFIA_GOON", "faction": "MAFIA", "cause": "DAY_VOTE"},
    ),
    _ev(
        13,
        "PhaseStarted",
        "NIGHT_1_ACTIONS",
        "SYSTEM",
        None,
        {"phase_kind": "NIGHT_ACTIONS", "day": 1, "round": 0},
    ),
    _ev(
        14,
        "PlayerEliminated",
        "NIGHT_1_ACTIONS",
        "PUBLIC",
        None,
        {"public_player_id": "P07", "role": "VILLAGER", "faction": "TOWN", "cause": "NIGHT_KILL"},
    ),
    _ev(
        15,
        "PhaseStarted",
        "DAY_2_VOTE",
        "SYSTEM",
        None,
        {"phase_kind": "DAY_VOTE", "day": 2, "round": 0},
    ),
    # Day 2 votes: P03/P04/P05/P06 vote P02 (MAFIA); P02 votes P03 (TOWN, inaccurate)
    _ev(16, "VoteSubmitted", "DAY_2_VOTE", "PUBLIC", "P03", {"target": "P02", "is_abstain": False}),
    _ev(17, "VoteSubmitted", "DAY_2_VOTE", "PUBLIC", "P04", {"target": "P02", "is_abstain": False}),
    _ev(18, "VoteSubmitted", "DAY_2_VOTE", "PUBLIC", "P05", {"target": "P02", "is_abstain": False}),
    _ev(19, "VoteSubmitted", "DAY_2_VOTE", "PUBLIC", "P06", {"target": "P02", "is_abstain": False}),
    _ev(20, "VoteSubmitted", "DAY_2_VOTE", "PUBLIC", "P02", {"target": "P03", "is_abstain": False}),
    _ev(
        21,
        "DayVoteResolved",
        "DAY_2_VOTE",
        "PUBLIC",
        None,
        {"eliminated": "P02", "vote_tally": {"P02": 4, "P03": 1}, "reason": "majority"},
    ),
    _ev(
        22,
        "PlayerEliminated",
        "DAY_2_VOTE",
        "PUBLIC",
        None,
        {"public_player_id": "P02", "role": "MAFIA_GOON", "faction": "MAFIA", "cause": "DAY_VOTE"},
    ),
    _ev(
        23,
        "GameTerminated",
        "DAY_2_VOTE",
        "PUBLIC",
        None,
        {"winner": "TOWN", "reason": "all_mafia_eliminated"},
    ),
]


# ---------------------------------------------------------------------------
# _phase_to_day
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phase, expected",
    [
        ("DAY_1_VOTE", 1),
        ("DAY_2_VOTE", 2),
        ("NIGHT_1_ACTIONS", 1),
        ("NIGHT_1_MAFIA_DISCUSSION", 1),
        ("DAY_1_DISCUSSION_ROUND_1", 1),
        ("NIGHT_0_MAFIA_INTRO", 0),
        ("SETUP", 0),
        ("TERMINAL", 0),
        ("DAY_5_VOTE", 5),
    ],
)
def test_phase_to_day(phase: str, expected: int) -> None:
    assert _phase_to_day(phase) == expected


# ---------------------------------------------------------------------------
# _extract_role_map
# ---------------------------------------------------------------------------


def test_extract_role_map_from_fixture() -> None:
    role_map = _extract_role_map(FIXTURE_GAME)
    assert role_map["P01"] == ("MAFIA_GOON", "MAFIA")
    assert role_map["P03"] == ("DETECTIVE", "TOWN")
    assert role_map["P07"] == ("VILLAGER", "TOWN")
    assert len(role_map) == 7


def test_extract_role_map_missing_returns_empty() -> None:
    events = [_ev(1, "GameCreated", "SETUP", "SYSTEM", None, {})]
    assert _extract_role_map(events) == {}


def test_extract_role_map_empty_returns_empty() -> None:
    assert _extract_role_map([]) == {}


# ---------------------------------------------------------------------------
# _extract_winner
# ---------------------------------------------------------------------------


def test_extract_winner_from_fixture() -> None:
    assert _extract_winner(FIXTURE_GAME) == "TOWN"


def test_extract_winner_no_terminal_returns_none() -> None:
    events = [
        _ev(
            1,
            "VoteSubmitted",
            "DAY_1_VOTE",
            "PUBLIC",
            "P01",
            {"target": "P02", "is_abstain": False},
        )
    ]
    assert _extract_winner(events) is None


def test_extract_winner_mafia() -> None:
    events = [
        _ev(
            1,
            "GameTerminated",
            "DAY_3_VOTE",
            "PUBLIC",
            None,
            {"winner": "MAFIA", "reason": "parity"},
        )
    ]
    assert _extract_winner(events) == "MAFIA"


def test_extract_winner_draw() -> None:
    events = [
        _ev(
            1,
            "GameTerminated",
            "DAY_5_VOTE",
            "PUBLIC",
            None,
            {"winner": "DRAW", "reason": "max_days"},
        )
    ]
    assert _extract_winner(events) == "DRAW"


# ---------------------------------------------------------------------------
# Per-role win rates
# ---------------------------------------------------------------------------


def test_role_win_rates_town_wins() -> None:
    analytics = compute_game_analytics(FIXTURE_GAME)
    by_role = {r.role: r for r in analytics.role_win_rates}

    assert by_role["MAFIA_GOON"].wins == 0
    assert by_role["MAFIA_GOON"].games == 2
    assert by_role["MAFIA_GOON"].rate == 0.0

    assert by_role["DETECTIVE"].wins == 1
    assert by_role["DETECTIVE"].games == 1
    assert by_role["DETECTIVE"].rate == 1.0

    assert by_role["DOCTOR"].wins == 1
    assert by_role["DOCTOR"].games == 1
    assert by_role["DOCTOR"].rate == 1.0

    assert by_role["VILLAGER"].wins == 3
    assert by_role["VILLAGER"].games == 3
    assert by_role["VILLAGER"].rate == 1.0


def test_role_win_rates_sorted_alphabetically() -> None:
    analytics = compute_game_analytics(FIXTURE_GAME)
    roles = [r.role for r in analytics.role_win_rates]
    assert roles == sorted(roles)


def test_role_win_rates_draw_has_no_winners() -> None:
    role_map = {"P01": ("MAFIA_GOON", "MAFIA"), "P02": ("VILLAGER", "TOWN")}
    rates = _compute_role_win_rates("DRAW", role_map)
    by_role = {r.role: r for r in rates}
    assert by_role["MAFIA_GOON"].wins == 0
    assert by_role["VILLAGER"].wins == 0


def test_role_win_rates_no_winner_returns_empty() -> None:
    role_map = {"P01": ("VILLAGER", "TOWN")}
    assert _compute_role_win_rates(None, role_map) == ()


def test_role_win_rates_no_roles_returns_empty() -> None:
    assert _compute_role_win_rates("TOWN", {}) == ()


def test_role_win_rate_zero_games_rate() -> None:
    r = RoleWinRate(role="VILLAGER", wins=0, games=0)
    assert r.rate == 0.0


# ---------------------------------------------------------------------------
# Voting accuracy
# ---------------------------------------------------------------------------


def test_voting_accuracy_fixture() -> None:
    analytics = compute_game_analytics(FIXTURE_GAME)
    va = analytics.voting_accuracy
    # Day 1: 5 accurate (P03,P04,P05,P06,P07 → P01/MAFIA) + 2 inaccurate (P01,P02 → P03/TOWN) = 7 total
    # Day 2: 4 accurate (P03,P04,P05,P06 → P02/MAFIA) + 1 inaccurate (P02 → P03/TOWN) = 5 total
    # Combined: 9 accurate / 12 total
    assert va.total_votes == 12
    assert va.accurate_votes == 9
    assert va.rate == pytest.approx(9 / 12)


def test_voting_accuracy_abstain_excluded() -> None:
    role_map = {"P01": ("MAFIA_GOON", "MAFIA"), "P02": ("VILLAGER", "TOWN")}
    events = [
        _ev(
            1, "VoteSubmitted", "DAY_1_VOTE", "PUBLIC", "P02", {"target": "P01", "is_abstain": True}
        ),
    ]
    va = _compute_voting_accuracy(events, role_map)
    assert va.total_votes == 0
    assert va.accurate_votes == 0
    assert va.rate == 0.0


def test_voting_accuracy_null_target_excluded() -> None:
    role_map = {"P01": ("MAFIA_GOON", "MAFIA")}
    events = [
        _ev(
            1, "VoteSubmitted", "DAY_1_VOTE", "PUBLIC", "P02", {"target": None, "is_abstain": False}
        ),
    ]
    va = _compute_voting_accuracy(events, role_map)
    assert va.total_votes == 0


def test_voting_accuracy_no_votes() -> None:
    analytics = compute_game_analytics(
        [_ev(1, "GameTerminated", "DAY_1_VOTE", "PUBLIC", None, {"winner": "TOWN", "reason": "x"})]
    )
    assert analytics.voting_accuracy.total_votes == 0
    assert analytics.voting_accuracy.rate == 0.0


def test_voting_accuracy_zero_total_rate() -> None:
    va = VotingAccuracy(total_votes=0, accurate_votes=0)
    assert va.rate == 0.0


def test_voting_accuracy_all_accurate() -> None:
    role_map = {"P01": ("MAFIA_GOON", "MAFIA")}
    events = [
        _ev(
            1,
            "VoteSubmitted",
            "DAY_1_VOTE",
            "PUBLIC",
            "P02",
            {"target": "P01", "is_abstain": False},
        ),
        _ev(
            2,
            "VoteSubmitted",
            "DAY_1_VOTE",
            "PUBLIC",
            "P03",
            {"target": "P01", "is_abstain": False},
        ),
    ]
    va = _compute_voting_accuracy(events, role_map)
    assert va.total_votes == 2
    assert va.accurate_votes == 2
    assert va.rate == 1.0


# ---------------------------------------------------------------------------
# Survival curves
# ---------------------------------------------------------------------------


def test_survival_curve_fixture() -> None:
    analytics = compute_game_analytics(FIXTURE_GAME)
    # Build lookup: (role, day) -> SurvivalPoint
    sp = {(p.role, p.day): p for p in analytics.survival_curve}

    # DETECTIVE (P03, never eliminated)
    assert sp[("DETECTIVE", 0)].alive_count == 1
    assert sp[("DETECTIVE", 1)].alive_count == 1
    assert sp[("DETECTIVE", 2)].alive_count == 1

    # DOCTOR (P04, never eliminated)
    assert sp[("DOCTOR", 0)].alive_count == 1
    assert sp[("DOCTOR", 1)].alive_count == 1
    assert sp[("DOCTOR", 2)].alive_count == 1

    # MAFIA_GOON: P01 eliminated day 1, P02 eliminated day 2
    assert sp[("MAFIA_GOON", 0)].alive_count == 2
    assert sp[("MAFIA_GOON", 1)].alive_count == 1
    assert sp[("MAFIA_GOON", 2)].alive_count == 0

    # VILLAGER: P07 eliminated night 1 (day 1), P05/P06 survive
    assert sp[("VILLAGER", 0)].alive_count == 3
    assert sp[("VILLAGER", 1)].alive_count == 2
    assert sp[("VILLAGER", 2)].alive_count == 2


def test_survival_curve_total_counts() -> None:
    analytics = compute_game_analytics(FIXTURE_GAME)
    sp = {(p.role, p.day): p for p in analytics.survival_curve}
    assert sp[("MAFIA_GOON", 0)].total_count == 2
    assert sp[("DETECTIVE", 0)].total_count == 1
    assert sp[("VILLAGER", 0)].total_count == 3


def test_survival_curve_fractions() -> None:
    analytics = compute_game_analytics(FIXTURE_GAME)
    sp = {(p.role, p.day): p for p in analytics.survival_curve}
    assert sp[("MAFIA_GOON", 1)].fraction == pytest.approx(0.5)
    assert sp[("MAFIA_GOON", 2)].fraction == 0.0
    assert sp[("DETECTIVE", 2)].fraction == 1.0


def test_survival_curve_zero_total_fraction() -> None:
    s = SurvivalPoint(role="VILLAGER", day=0, alive_count=0, total_count=0)
    assert s.fraction == 0.0


def test_survival_curve_no_roles_empty() -> None:
    events = [
        _ev(1, "GameTerminated", "DAY_1_VOTE", "PUBLIC", None, {"winner": "TOWN", "reason": "x"})
    ]
    result = _compute_survival_curve(events, {})
    assert result == ()


# ---------------------------------------------------------------------------
# compute_game_analytics — end-to-end
# ---------------------------------------------------------------------------


def test_compute_game_analytics_returns_game_analytics() -> None:
    result = compute_game_analytics(FIXTURE_GAME)
    assert isinstance(result, GameAnalytics)


def test_compute_game_analytics_winner() -> None:
    result = compute_game_analytics(FIXTURE_GAME)
    assert result.winner == "TOWN"


def test_compute_game_analytics_empty_events() -> None:
    result = compute_game_analytics([])
    assert result.winner is None
    assert result.role_win_rates == ()
    assert result.voting_accuracy == VotingAccuracy(0, 0)
    assert result.survival_curve == ()


def test_compute_game_analytics_no_roles_no_win_rates() -> None:
    events = [
        _ev(
            1,
            "VoteSubmitted",
            "DAY_1_VOTE",
            "PUBLIC",
            "P01",
            {"target": "P02", "is_abstain": False},
        ),
        _ev(2, "GameTerminated", "DAY_1_VOTE", "PUBLIC", None, {"winner": "TOWN", "reason": "x"}),
    ]
    result = compute_game_analytics(events)
    assert result.role_win_rates == ()
    assert result.survival_curve == ()
    # Vote still counted even without role context (no mafia → 0 accurate)
    assert result.voting_accuracy.total_votes == 1
    assert result.voting_accuracy.accurate_votes == 0
