"""Tests for the pure event reducer."""

from __future__ import annotations

from typing import cast

import pytest

from padrino.core.engine.events import (
    ActionTimedOut,
    ActionTimedOutPayload,
    DayVoteResolved,
    DayVoteResolvedPayload,
    DetectiveResultDelivered,
    DetectiveResultDeliveredPayload,
    Event,
    GameCreated,
    GameCreatedPayload,
    GameTerminated,
    GameTerminatedPayload,
    InvestigateSubmitted,
    InvestigateSubmittedPayload,
    MafiaKillVoteSubmitted,
    MafiaKillVoteSubmittedPayload,
    NightResolved,
    NightResolvedPayload,
    OutputInvalid,
    OutputInvalidPayload,
    OutputTruncated,
    OutputTruncatedPayload,
    PhaseResolved,
    PhaseResolvedPayload,
    PhaseStarted,
    PhaseStartedPayload,
    PlayerEliminated,
    PlayerEliminatedPayload,
    PrivateMessageSubmitted,
    PrivateMessageSubmittedPayload,
    ProtectSubmitted,
    ProtectSubmittedPayload,
    PublicMessageSubmitted,
    PublicMessageSubmittedPayload,
    RolesAssigned,
    RolesAssignedPayload,
    SeatAssignment,
    VoteSubmitted,
    VoteSubmittedPayload,
)
from padrino.core.engine.reducer import apply_event, initial_state
from padrino.core.engine.state import GameState, Phase, QueuedInspection, Seat
from padrino.core.enums import Faction, PhaseKind, Role


def _assignments() -> tuple[SeatAssignment, ...]:
    return (
        SeatAssignment(
            public_player_id="P01", seat_index=0, role=Role.MAFIA_GOON, faction=Faction.MAFIA
        ),
        SeatAssignment(
            public_player_id="P02", seat_index=1, role=Role.MAFIA_GOON, faction=Faction.MAFIA
        ),
        SeatAssignment(
            public_player_id="P03", seat_index=2, role=Role.DETECTIVE, faction=Faction.TOWN
        ),
        SeatAssignment(
            public_player_id="P04", seat_index=3, role=Role.DOCTOR, faction=Faction.TOWN
        ),
        SeatAssignment(
            public_player_id="P05", seat_index=4, role=Role.VILLAGER, faction=Faction.TOWN
        ),
        SeatAssignment(
            public_player_id="P06", seat_index=5, role=Role.VILLAGER, faction=Faction.TOWN
        ),
        SeatAssignment(
            public_player_id="P07", seat_index=6, role=Role.VILLAGER, faction=Faction.TOWN
        ),
    )


def _bootstrapped() -> GameState:
    state = initial_state()
    state = apply_event(
        state,
        GameCreated(
            sequence=0,
            phase="SETUP_0_0",
            payload=GameCreatedPayload(
                ruleset_id="mini7_v1",
                game_id="G-1",
                game_seed="seed-abc",
                player_count=7,
            ),
        ),
    )
    state = apply_event(
        state,
        RolesAssigned(
            sequence=1,
            phase="SETUP_0_0",
            payload=RolesAssignedPayload(assignments=_assignments()),
        ),
    )
    return state


def test_initial_state_is_empty() -> None:
    s = initial_state()
    assert s.ruleset_id == ""
    assert s.game_id == ""
    assert s.game_seed == ""
    assert s.seats == ()
    assert s.day == 0
    assert s.current_phase.kind is PhaseKind.SETUP
    assert s.terminal_result is None
    assert s.terminal_reason is None


def test_game_created_initializes_ids() -> None:
    state = apply_event(
        initial_state(),
        GameCreated(
            sequence=0,
            phase="SETUP_0_0",
            payload=GameCreatedPayload(
                ruleset_id="mini7_v1",
                game_id="G-123",
                game_seed="abc",
                player_count=7,
            ),
        ),
    )
    assert state.ruleset_id == "mini7_v1"
    assert state.game_id == "G-123"
    assert state.game_seed == "abc"
    assert state.seats == ()


def test_roles_assigned_populates_seats() -> None:
    state = _bootstrapped()
    assert len(state.seats) == 7
    assert all(s.alive for s in state.seats)
    assert all(s.death_phase is None for s in state.seats)
    assert all(s.last_protected_target is None for s in state.seats)
    assert all(s.queued_inspection_result is None for s in state.seats)
    assert [s.public_player_id for s in state.seats] == [f"P0{i + 1}" for i in range(7)]
    assert state.seats[0].role is Role.MAFIA_GOON
    assert state.seats[2].role is Role.DETECTIVE


def test_phase_started_updates_current_phase_and_day() -> None:
    state = _bootstrapped()
    state = apply_event(
        state,
        PhaseStarted(
            sequence=2,
            phase="DAY_1_DISCUSSION_ROUND_1",
            payload=PhaseStartedPayload(phase_kind="DAY_DISCUSSION", day=1, round=1),
        ),
    )
    assert state.current_phase == Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=1)
    assert state.day == 1


def test_player_eliminated_marks_seat_dead() -> None:
    state = _bootstrapped()
    state = apply_event(
        state,
        PlayerEliminated(
            sequence=5,
            phase="DAY_1_VOTE",
            payload=PlayerEliminatedPayload(
                public_player_id="P05",
                role=Role.VILLAGER,
                faction=Faction.TOWN,
                cause="DAY_VOTE",
            ),
        ),
    )
    target = state.seat_by_public_id("P05")
    assert target is not None
    assert target.alive is False
    assert target.death_phase == "DAY_1_VOTE"
    # Other seats unaffected.
    others = [s for s in state.seats if s.public_player_id != "P05"]
    assert all(s.alive for s in others)
    assert all(s.death_phase is None for s in others)


def test_night_resolved_spends_successful_janitor_clean_shot() -> None:
    state = _bootstrapped()
    janitor = state.seats[1].model_copy(update={"role": Role.JANITOR})
    state = state.model_copy(update={"seats": (state.seats[0], janitor, *state.seats[2:])})

    state = apply_event(
        state,
        NightResolved(
            sequence=5,
            phase="NIGHT_1_ACTIONS",
            payload=NightResolvedPayload(
                eliminated="P05",
                protected=None,
                mafia_kill_target="P05",
                cleaned_deaths=("P05",),
                clean_spent_actor_ids=("P02",),
            ),
        ),
    )

    spent = state.seat_by_public_id("P02")
    assert spent is not None
    assert spent.janitor_clean_shots_remaining == 0


def test_detective_result_delivered_sets_queue() -> None:
    state = _bootstrapped()
    state = apply_event(
        state,
        DetectiveResultDelivered(
            sequence=10,
            phase="DAY_2_DISCUSSION_ROUND_1",
            actor_player_id="P03",
            payload=DetectiveResultDeliveredPayload(target="P01", finding="MAFIA"),
        ),
    )
    detective = state.seat_by_public_id("P03")
    assert detective is not None
    assert detective.queued_inspection_result == QueuedInspection(target="P01", finding="MAFIA")
    # Other seats untouched.
    others = [s for s in state.seats if s.public_player_id != "P03"]
    assert all(s.queued_inspection_result is None for s in others)


def test_protect_submitted_records_last_protected_target() -> None:
    state = _bootstrapped()
    state = apply_event(
        state,
        ProtectSubmitted(
            sequence=4,
            phase="NIGHT_1_ACTIONS",
            actor_player_id="P04",
            payload=ProtectSubmittedPayload(target="P03"),
        ),
    )
    doctor = state.seat_by_public_id("P04")
    assert doctor is not None
    assert doctor.last_protected_target == "P03"
    others = [s for s in state.seats if s.public_player_id != "P04"]
    assert all(s.last_protected_target is None for s in others)


def test_game_terminated_sets_terminal_fields() -> None:
    state = _bootstrapped()
    state = apply_event(
        state,
        GameTerminated(
            sequence=99,
            phase="TERMINAL_5_0",
            payload=GameTerminatedPayload(winner="TOWN", reason="ALL_MAFIA_ELIMINATED"),
        ),
    )
    assert state.terminal_result == "TOWN"
    assert state.terminal_reason == "ALL_MAFIA_ELIMINATED"


@pytest.mark.parametrize(
    "event",
    [
        PublicMessageSubmitted(
            sequence=3,
            phase="DAY_1_DISCUSSION_ROUND_1",
            actor_player_id="P01",
            payload=PublicMessageSubmittedPayload(text="hi", round_index=1),
        ),
        PrivateMessageSubmitted(
            sequence=4,
            phase="NIGHT_1_MAFIA_DISCUSSION",
            actor_player_id="P01",
            payload=PrivateMessageSubmittedPayload(text="kill P03", channel_id="mafia"),
        ),
        VoteSubmitted(
            sequence=5,
            phase="DAY_1_VOTE",
            actor_player_id="P01",
            payload=VoteSubmittedPayload(target="P05", is_abstain=False),
        ),
        MafiaKillVoteSubmitted(
            sequence=6,
            phase="NIGHT_1_ACTIONS",
            actor_player_id="P01",
            payload=MafiaKillVoteSubmittedPayload(target="P03"),
        ),
        InvestigateSubmitted(
            sequence=7,
            phase="NIGHT_1_ACTIONS",
            actor_player_id="P03",
            payload=InvestigateSubmittedPayload(target="P01"),
        ),
        ActionTimedOut(
            sequence=8,
            phase="NIGHT_1_ACTIONS",
            actor_player_id="P03",
            payload=ActionTimedOutPayload(expected_action_type="INVESTIGATE", defaulted_to="NOOP"),
        ),
        OutputTruncated(
            sequence=9,
            phase="DAY_1_DISCUSSION_ROUND_1",
            actor_player_id="P01",
            payload=OutputTruncatedPayload(reason="too_long", raw_byte_length=999),
        ),
        OutputInvalid(
            sequence=10,
            phase="DAY_1_DISCUSSION_ROUND_1",
            actor_player_id="P01",
            payload=OutputInvalidPayload(reason="schema", validation_errors=("missing field",)),
        ),
        DayVoteResolved(
            sequence=11,
            phase="DAY_1_VOTE",
            payload=DayVoteResolvedPayload(
                eliminated="P05", vote_tally={"P05": 4}, reason="UNIQUE_PLURALITY"
            ),
        ),
        NightResolved(
            sequence=12,
            phase="NIGHT_1_ACTIONS",
            payload=NightResolvedPayload(eliminated="P03", protected=None, mafia_kill_target="P03"),
        ),
        PhaseResolved(
            sequence=13,
            phase="DAY_1_VOTE",
            payload=PhaseResolvedPayload(resolved_phase="DAY_1_VOTE"),
        ),
    ],
)
def test_recorded_only_events_do_not_mutate_state(event: Event) -> None:
    state = _bootstrapped()
    after = apply_event(state, event)
    assert after == state


def test_unknown_event_type_raises() -> None:
    state = _bootstrapped()

    class _NotAnEvent:
        event_type = "Bogus"
        sequence = 0
        phase = "SETUP_0_0"
        visibility = "SYSTEM"
        actor_player_id: str | None = None
        payload: object = None

    with pytest.raises(ValueError, match="unknown event type"):
        apply_event(state, cast(Event, _NotAnEvent()))


def test_apply_event_returns_new_state_does_not_mutate() -> None:
    state = _bootstrapped()
    before_seats = state.seats
    after = apply_event(
        state,
        PlayerEliminated(
            sequence=1,
            phase="DAY_1_VOTE",
            payload=PlayerEliminatedPayload(
                public_player_id="P05",
                role=Role.VILLAGER,
                faction=Faction.TOWN,
                cause="DAY_VOTE",
            ),
        ),
    )
    assert after is not state
    assert state.seats is before_seats
    assert all(s.alive for s in state.seats)


def test_apply_event_handles_every_known_event_type() -> None:
    """Exhaustiveness guard: every Event class must dispatch without raising."""
    state = _bootstrapped()
    events: list[Event] = [
        GameCreated(
            sequence=0,
            phase="SETUP_0_0",
            payload=GameCreatedPayload(
                ruleset_id="mini7_v1", game_id="G-1", game_seed="s", player_count=7
            ),
        ),
        RolesAssigned(
            sequence=1, phase="SETUP_0_0", payload=RolesAssignedPayload(assignments=_assignments())
        ),
        PhaseStarted(
            sequence=2,
            phase="DAY_1_DISCUSSION_ROUND_1",
            payload=PhaseStartedPayload(phase_kind="DAY_DISCUSSION", day=1, round=1),
        ),
        PublicMessageSubmitted(
            sequence=3,
            phase="DAY_1_DISCUSSION_ROUND_1",
            actor_player_id="P01",
            payload=PublicMessageSubmittedPayload(text="hi"),
        ),
        PrivateMessageSubmitted(
            sequence=4,
            phase="NIGHT_1_MAFIA_DISCUSSION",
            actor_player_id="P01",
            payload=PrivateMessageSubmittedPayload(text="x", channel_id="mafia"),
        ),
        VoteSubmitted(
            sequence=5,
            phase="DAY_1_VOTE",
            actor_player_id="P01",
            payload=VoteSubmittedPayload(target="P05", is_abstain=False),
        ),
        MafiaKillVoteSubmitted(
            sequence=6,
            phase="NIGHT_1_ACTIONS",
            actor_player_id="P01",
            payload=MafiaKillVoteSubmittedPayload(target="P03"),
        ),
        ProtectSubmitted(
            sequence=7,
            phase="NIGHT_1_ACTIONS",
            actor_player_id="P04",
            payload=ProtectSubmittedPayload(target="P03"),
        ),
        InvestigateSubmitted(
            sequence=8,
            phase="NIGHT_1_ACTIONS",
            actor_player_id="P03",
            payload=InvestigateSubmittedPayload(target="P01"),
        ),
        ActionTimedOut(
            sequence=9,
            phase="NIGHT_1_ACTIONS",
            actor_player_id="P03",
            payload=ActionTimedOutPayload(expected_action_type="INVESTIGATE", defaulted_to="NOOP"),
        ),
        OutputTruncated(
            sequence=10,
            phase="DAY_1_DISCUSSION_ROUND_1",
            actor_player_id="P01",
            payload=OutputTruncatedPayload(reason="r", raw_byte_length=1),
        ),
        OutputInvalid(
            sequence=11,
            phase="DAY_1_DISCUSSION_ROUND_1",
            actor_player_id="P01",
            payload=OutputInvalidPayload(reason="r", validation_errors=()),
        ),
        DayVoteResolved(
            sequence=12,
            phase="DAY_1_VOTE",
            payload=DayVoteResolvedPayload(eliminated=None, vote_tally={}, reason="ALL_ABSTAIN"),
        ),
        NightResolved(
            sequence=13,
            phase="NIGHT_1_ACTIONS",
            payload=NightResolvedPayload(eliminated=None, protected=None, mafia_kill_target=None),
        ),
        DetectiveResultDelivered(
            sequence=14,
            phase="DAY_2_DISCUSSION_ROUND_1",
            actor_player_id="P03",
            payload=DetectiveResultDeliveredPayload(target="P01", finding="MAFIA"),
        ),
        PlayerEliminated(
            sequence=15,
            phase="DAY_1_VOTE",
            payload=PlayerEliminatedPayload(
                public_player_id="P05", role=Role.VILLAGER, faction=Faction.TOWN, cause="DAY_VOTE"
            ),
        ),
        PhaseResolved(
            sequence=16,
            phase="DAY_1_VOTE",
            payload=PhaseResolvedPayload(resolved_phase="DAY_1_VOTE"),
        ),
        GameTerminated(
            sequence=17,
            phase="TERMINAL_5_0",
            payload=GameTerminatedPayload(winner="DRAW", reason="MAX_DAYS_REACHED"),
        ),
    ]
    # Each event must dispatch without raising.
    for e in events:
        apply_event(state, e)


def test_full_event_sequence_replay() -> None:
    """Hand-crafted sequence: bootstrap, eliminate one mafia, terminate town-win."""
    s = initial_state()
    s = apply_event(
        s,
        GameCreated(
            sequence=0,
            phase="SETUP_0_0",
            payload=GameCreatedPayload(
                ruleset_id="mini7_v1", game_id="G-1", game_seed="seed", player_count=7
            ),
        ),
    )
    s = apply_event(
        s,
        RolesAssigned(
            sequence=1, phase="SETUP_0_0", payload=RolesAssignedPayload(assignments=_assignments())
        ),
    )
    s = apply_event(
        s,
        PhaseStarted(
            sequence=2,
            phase="DAY_1_DISCUSSION_ROUND_1",
            payload=PhaseStartedPayload(phase_kind="DAY_DISCUSSION", day=1, round=1),
        ),
    )
    # Vote out P01 (mafia) on day 1.
    s = apply_event(
        s,
        PhaseStarted(
            sequence=3,
            phase="DAY_1_VOTE",
            payload=PhaseStartedPayload(phase_kind="DAY_VOTE", day=1, round=0),
        ),
    )
    s = apply_event(
        s,
        PlayerEliminated(
            sequence=4,
            phase="DAY_1_VOTE",
            payload=PlayerEliminatedPayload(
                public_player_id="P01",
                role=Role.MAFIA_GOON,
                faction=Faction.MAFIA,
                cause="DAY_VOTE",
            ),
        ),
    )
    # Night 1: doctor protects, detective investigates, mafia kills.
    s = apply_event(
        s,
        PhaseStarted(
            sequence=5,
            phase="NIGHT_1_ACTIONS",
            payload=PhaseStartedPayload(phase_kind="NIGHT_ACTIONS", day=1, round=0),
        ),
    )
    s = apply_event(
        s,
        ProtectSubmitted(
            sequence=6,
            phase="NIGHT_1_ACTIONS",
            actor_player_id="P04",
            payload=ProtectSubmittedPayload(target="P03"),
        ),
    )
    s = apply_event(
        s,
        PlayerEliminated(
            sequence=7,
            phase="NIGHT_1_ACTIONS",
            payload=PlayerEliminatedPayload(
                public_player_id="P02",
                role=Role.MAFIA_GOON,
                faction=Faction.MAFIA,
                cause="MAFIA_KILL",
            ),
        ),
    )
    # Terminate.
    s = apply_event(
        s,
        GameTerminated(
            sequence=8,
            phase="TERMINAL_1_0",
            payload=GameTerminatedPayload(winner="TOWN", reason="ALL_MAFIA_ELIMINATED"),
        ),
    )

    # Final state assertions.
    assert s.ruleset_id == "mini7_v1"
    assert s.game_id == "G-1"
    assert s.day == 1
    assert s.current_phase == Phase(kind=PhaseKind.NIGHT_ACTIONS, day=1, round=0)
    p01 = s.seat_by_public_id("P01")
    p02 = s.seat_by_public_id("P02")
    p03 = s.seat_by_public_id("P03")
    p04 = s.seat_by_public_id("P04")
    assert p01 is not None and not p01.alive and p01.death_phase == "DAY_1_VOTE"
    assert p02 is not None and not p02.alive and p02.death_phase == "NIGHT_1_ACTIONS"
    assert p03 is not None and p03.alive
    assert p04 is not None and p04.last_protected_target == "P03"
    assert s.terminal_result == "TOWN"
    assert s.terminal_reason == "ALL_MAFIA_ELIMINATED"


def test_reducer_has_no_forbidden_imports() -> None:
    """AST guard: reducer must not import db/llm/runner/random/secrets."""
    import ast
    from pathlib import Path

    src = Path("src/padrino/core/engine/reducer.py").read_text()
    tree = ast.parse(src)
    forbidden = {
        "random",
        "secrets",
        "padrino.db",
        "padrino.llm",
        "padrino.runner",
        "padrino.api",
        "sqlalchemy",
        "litellm",
        "httpx",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in forbidden, alias.name
        elif isinstance(node, ast.ImportFrom):
            assert node.module not in forbidden, node.module


def _build_seat(pid: str, idx: int, role: Role, faction: Faction) -> Seat:
    return Seat(
        public_player_id=pid,
        seat_index=idx,
        role=role,
        faction=faction,
        alive=True,
    )


def test_eliminating_nonexistent_seat_is_noop() -> None:
    """Defensive: eliminating a public id not in seats leaves state alone."""
    state = _bootstrapped()
    after = apply_event(
        state,
        PlayerEliminated(
            sequence=1,
            phase="DAY_1_VOTE",
            payload=PlayerEliminatedPayload(
                public_player_id="P99", role=Role.VILLAGER, faction=Faction.TOWN, cause="DAY_VOTE"
            ),
        ),
    )
    assert after.seats == state.seats
