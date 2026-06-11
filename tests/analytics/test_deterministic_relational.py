"""Tests for deterministic relational analytics (US-103).

Covers:
  - claim/counter-claim extraction from structured RoleClaimed events
  - head-to-head win matrices between agents (cross-faction pairs only)
"""

from __future__ import annotations

from typing import Any

from padrino.analytics.deterministic import (
    ClaimAnalysis,
    ClaimRecord,
    CounterClaimGroup,
    HeadToHeadEntry,
    compute_claim_analysis,
    compute_head_to_head_matrix,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


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


def _roles_assigned(assignments: list[dict[str, str]]) -> dict[str, Any]:
    return _ev(1, "RolesAssigned", "SETUP", "SYSTEM", None, {"assignments": assignments})


def _game_terminated(winner: str) -> dict[str, Any]:
    return _ev(99, "GameTerminated", "TERMINAL", "PUBLIC", None, {"winner": winner, "reason": "x"})


def _role_claimed(seq: int, phase: str, actor: str, claimed_role: str) -> dict[str, Any]:
    return _ev(seq, "RoleClaimed", phase, "PUBLIC", actor, {"claimed_role": claimed_role})


# ---------------------------------------------------------------------------
# Fixture game: mini7_v1 with claim events
# ---------------------------------------------------------------------------
# P01+P02=MAFIA, P03=DETECTIVE/TOWN, P04=DOCTOR/TOWN, P05+P06+P07=VILLAGER/TOWN
# Agents: agent-A..G map 1:1 to players P01..P07

_ROLE_ASSIGNMENTS: list[dict[str, str]] = [
    {"public_player_id": "P01", "role": "MAFIA_GOON", "faction": "MAFIA"},
    {"public_player_id": "P02", "role": "MAFIA_GOON", "faction": "MAFIA"},
    {"public_player_id": "P03", "role": "DETECTIVE", "faction": "TOWN"},
    {"public_player_id": "P04", "role": "DOCTOR", "faction": "TOWN"},
    {"public_player_id": "P05", "role": "VILLAGER", "faction": "TOWN"},
    {"public_player_id": "P06", "role": "VILLAGER", "faction": "TOWN"},
    {"public_player_id": "P07", "role": "VILLAGER", "faction": "TOWN"},
]

# agent_map: player_id -> agent_build_id (arbitrary UUIDs for test)
AGENT_MAP: dict[str, str] = {
    "P01": "agent-A",
    "P02": "agent-B",
    "P03": "agent-C",
    "P04": "agent-D",
    "P05": "agent-E",
    "P06": "agent-F",
    "P07": "agent-G",
}

# Full fixture game with role claims.
# P03 (true DETECTIVE) claims DETECTIVE. P01 (MAFIA) counter-claims DETECTIVE.
# P04 (DOCTOR) claims DOCTOR. No counter-claim on DOCTOR.
FIXTURE_GAME: list[dict[str, Any]] = [
    _roles_assigned(_ROLE_ASSIGNMENTS),
    _role_claimed(2, "DAY_1_DISCUSSION", "P03", "DETECTIVE"),
    _role_claimed(3, "DAY_1_DISCUSSION", "P04", "DOCTOR"),
    _role_claimed(4, "DAY_1_DISCUSSION", "P01", "DETECTIVE"),  # counter-claim
    _game_terminated("TOWN"),
]


# ---------------------------------------------------------------------------
# compute_claim_analysis — claim tracking
# ---------------------------------------------------------------------------


def test_claim_analysis_returns_claim_analysis() -> None:
    result = compute_claim_analysis(FIXTURE_GAME)
    assert isinstance(result, ClaimAnalysis)


def test_claim_analysis_all_claims_extracted() -> None:
    result = compute_claim_analysis(FIXTURE_GAME)
    assert len(result.claims) == 3


def test_claim_analysis_claim_fields() -> None:
    result = compute_claim_analysis(FIXTURE_GAME)
    first = result.claims[0]
    assert isinstance(first, ClaimRecord)
    assert first.player_id == "P03"
    assert first.claimed_role == "DETECTIVE"
    assert first.sequence == 2
    assert first.phase == "DAY_1_DISCUSSION"


def test_claim_analysis_claims_in_sequence_order() -> None:
    result = compute_claim_analysis(FIXTURE_GAME)
    sequences = [c.sequence for c in result.claims]
    assert sequences == sorted(sequences)


def test_claim_analysis_counter_claim_detected() -> None:
    result = compute_claim_analysis(FIXTURE_GAME)
    assert len(result.counter_claims) == 1
    cc = result.counter_claims[0]
    assert isinstance(cc, CounterClaimGroup)
    assert cc.claimed_role == "DETECTIVE"
    assert set(cc.claimants) == {"P01", "P03"}


def test_claim_analysis_counter_claim_claimants_sorted() -> None:
    result = compute_claim_analysis(FIXTURE_GAME)
    cc = result.counter_claims[0]
    assert list(cc.claimants) == sorted(cc.claimants)


def test_claim_analysis_no_counter_claim_for_unique_role() -> None:
    result = compute_claim_analysis(FIXTURE_GAME)
    roles_with_counter = {cc.claimed_role for cc in result.counter_claims}
    assert "DOCTOR" not in roles_with_counter


def test_claim_analysis_no_claims_returns_empty() -> None:
    events: list[dict[str, Any]] = [_game_terminated("TOWN")]
    result = compute_claim_analysis(events)
    assert result.claims == ()
    assert result.counter_claims == ()


def test_claim_analysis_ignores_non_role_claimed_events() -> None:
    events = [
        _ev(
            1,
            "VoteSubmitted",
            "DAY_1_VOTE",
            "PUBLIC",
            "P03",
            {"target": "P01", "is_abstain": False},
        ),
        _role_claimed(2, "DAY_1_DISCUSSION", "P03", "DETECTIVE"),
    ]
    result = compute_claim_analysis(events)
    assert len(result.claims) == 1


def test_claim_analysis_ignores_event_without_actor() -> None:
    events = [
        _ev(1, "RoleClaimed", "DAY_1", "PUBLIC", None, {"claimed_role": "DETECTIVE"}),
    ]
    result = compute_claim_analysis(events)
    assert result.claims == ()


def test_claim_analysis_triple_claim_produces_one_counter_group() -> None:
    events = [
        _role_claimed(1, "DAY_1", "P01", "DETECTIVE"),
        _role_claimed(2, "DAY_1", "P02", "DETECTIVE"),
        _role_claimed(3, "DAY_1", "P03", "DETECTIVE"),
    ]
    result = compute_claim_analysis(events)
    assert len(result.counter_claims) == 1
    assert len(result.counter_claims[0].claimants) == 3


# ---------------------------------------------------------------------------
# compute_head_to_head_matrix — matrix symmetry and counts
# ---------------------------------------------------------------------------


def test_h2h_returns_tuple_of_head_to_head_entries() -> None:
    entries = compute_head_to_head_matrix(FIXTURE_GAME, AGENT_MAP)
    assert isinstance(entries, tuple)
    assert all(isinstance(e, HeadToHeadEntry) for e in entries)


def test_h2h_canonical_ordering_no_reversed_pairs() -> None:
    """Matrix symmetry: agent_a < agent_b for every entry (no duplicates)."""
    entries = compute_head_to_head_matrix(FIXTURE_GAME, AGENT_MAP)
    assert len(entries) > 0
    for e in entries:
        assert e.agent_a < e.agent_b, f"Non-canonical pair: ({e.agent_a}, {e.agent_b})"


def test_h2h_no_duplicate_pairs() -> None:
    entries = compute_head_to_head_matrix(FIXTURE_GAME, AGENT_MAP)
    pairs = [(e.agent_a, e.agent_b) for e in entries]
    assert len(pairs) == len(set(pairs))


def test_h2h_cross_faction_only() -> None:
    """Same-faction pairs must not appear in the matrix."""
    entries = compute_head_to_head_matrix(FIXTURE_GAME, AGENT_MAP)
    # TOWN agents: C, D, E, F, G — MAFIA agents: A, B
    town_agents = {"agent-C", "agent-D", "agent-E", "agent-F", "agent-G"}
    mafia_agents = {"agent-A", "agent-B"}
    for e in entries:
        both_town = e.agent_a in town_agents and e.agent_b in town_agents
        both_mafia = e.agent_a in mafia_agents and e.agent_b in mafia_agents
        assert not both_town, f"Same-faction pair found: {e}"
        assert not both_mafia, f"Same-faction pair found: {e}"


def test_h2h_town_wins_town_agents_get_wins() -> None:
    """TOWN wins → every TOWN-vs-MAFIA pair has a_wins or b_wins == 1."""
    entries = compute_head_to_head_matrix(FIXTURE_GAME, AGENT_MAP)
    town_agents = {"agent-C", "agent-D", "agent-E", "agent-F", "agent-G"}
    mafia_agents = {"agent-A", "agent-B"}
    for e in entries:
        is_cross = (e.agent_a in town_agents and e.agent_b in mafia_agents) or (
            e.agent_a in mafia_agents and e.agent_b in town_agents
        )
        if not is_cross:
            continue
        # Exactly one faction won
        assert e.a_wins + e.b_wins == 1
        # The TOWN agent should hold the win
        if e.agent_a in town_agents:
            assert e.a_wins == 1 and e.b_wins == 0
        else:
            assert e.b_wins == 1 and e.a_wins == 0


def test_h2h_count_of_cross_faction_pairs() -> None:
    """mini7_v1: 5 TOWN x 2 MAFIA = 10 cross-faction pairs."""
    entries = compute_head_to_head_matrix(FIXTURE_GAME, AGENT_MAP)
    assert len(entries) == 10


def test_h2h_draw_no_wins() -> None:
    events = [
        _roles_assigned(_ROLE_ASSIGNMENTS),
        _game_terminated("DRAW"),
    ]
    entries = compute_head_to_head_matrix(events, AGENT_MAP)
    for e in entries:
        assert e.a_wins == 0 and e.b_wins == 0


def test_h2h_empty_agent_map_returns_empty() -> None:
    entries = compute_head_to_head_matrix(FIXTURE_GAME, {})
    assert entries == ()


def test_h2h_no_roles_assigned_returns_empty() -> None:
    events = [_game_terminated("TOWN")]
    entries = compute_head_to_head_matrix(events, AGENT_MAP)
    assert entries == ()


def test_h2h_no_game_terminated_returns_empty() -> None:
    events = [_roles_assigned(_ROLE_ASSIGNMENTS)]
    entries = compute_head_to_head_matrix(events, AGENT_MAP)
    assert entries == ()


def test_h2h_mafia_win_mafia_agents_get_wins() -> None:
    events = [
        _roles_assigned(_ROLE_ASSIGNMENTS),
        _game_terminated("MAFIA"),
    ]
    entries = compute_head_to_head_matrix(events, AGENT_MAP)
    town_agents = {"agent-C", "agent-D", "agent-E", "agent-F", "agent-G"}
    mafia_agents = {"agent-A", "agent-B"}
    for e in entries:
        if e.agent_a in mafia_agents and e.agent_b in town_agents:
            assert e.a_wins == 1 and e.b_wins == 0
        elif e.agent_a in town_agents and e.agent_b in mafia_agents:
            assert e.a_wins == 0 and e.b_wins == 1


def test_h2h_partial_agent_map() -> None:
    """Agents not in agent_map are excluded from the matrix."""
    partial_map = {"P01": "agent-A", "P03": "agent-C"}  # one MAFIA + one TOWN
    entries = compute_head_to_head_matrix(FIXTURE_GAME, partial_map)
    assert len(entries) == 1
    e = entries[0]
    assert {e.agent_a, e.agent_b} == {"agent-A", "agent-C"}
