"""Tests for the per-seat observation builder."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from padrino.core.engine.event_log import EventLog
from padrino.core.engine.events import (
    DetectiveResultDelivered,
    DetectiveResultDeliveredPayload,
    Event,
    MafiaKillVoteSubmitted,
    MafiaKillVoteSubmittedPayload,
    NightFeedbackDelivered,
    NightFeedbackDeliveredPayload,
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
    VoteSubmitted,
    VoteSubmittedPayload,
)
from padrino.core.engine.state import GameState, Phase, Seat
from padrino.core.enums import ActionType, Faction, PhaseKind, Role
from padrino.core.observations import (
    Observation,
    build_observation,
    format_phase_id,
)
from padrino.core.rulesets import mini7_v1


def _seven_seats(
    *,
    dead: tuple[str, ...] = (),
    death_phase: str = "DAY_1_VOTE",
    last_protected_target: str | None = None,
) -> tuple[Seat, ...]:
    spec: list[tuple[str, int, Role, Faction]] = [
        ("P01", 0, Role.MAFIA_GOON, Faction.MAFIA),
        ("P02", 1, Role.MAFIA_GOON, Faction.MAFIA),
        ("P03", 2, Role.DETECTIVE, Faction.TOWN),
        ("P04", 3, Role.DOCTOR, Faction.TOWN),
        ("P05", 4, Role.VILLAGER, Faction.TOWN),
        ("P06", 5, Role.VILLAGER, Faction.TOWN),
        ("P07", 6, Role.VILLAGER, Faction.TOWN),
    ]
    seats: list[Seat] = []
    for public_id, idx, role, faction in spec:
        is_dead = public_id in dead
        seats.append(
            Seat(
                public_player_id=public_id,
                seat_index=idx,
                role=role,
                faction=faction,
                alive=not is_dead,
                death_phase=death_phase if is_dead else None,
                last_protected_target=(last_protected_target if role is Role.DOCTOR else None),
            )
        )
    return tuple(seats)


def _state(
    phase: Phase,
    *,
    seats: tuple[Seat, ...] | None = None,
) -> GameState:
    return GameState(
        ruleset_id="mini7_v1",
        game_id="G-obs-test",
        game_seed="seed-obs",
        current_phase=phase,
        seats=seats if seats is not None else _seven_seats(),
        day=phase.day,
    )


def _append(log: EventLog, ev: Event) -> None:
    log.append(ev.model_dump(mode="json"))


# --------------------------------------------------------------------------- #
# Phase id formatting
# --------------------------------------------------------------------------- #


def test_format_phase_id_setup() -> None:
    assert format_phase_id(Phase(kind=PhaseKind.SETUP, day=0, round=0)) == "SETUP"


def test_format_phase_id_night_zero_intro() -> None:
    assert (
        format_phase_id(Phase(kind=PhaseKind.NIGHT_0_MAFIA_INTRO, day=0, round=0))
        == "NIGHT_0_MAFIA_INTRO"
    )


def test_format_phase_id_day_discussion_round() -> None:
    assert (
        format_phase_id(Phase(kind=PhaseKind.DAY_DISCUSSION, day=2, round=3))
        == "DAY_2_DISCUSSION_ROUND_3"
    )


def test_format_phase_id_day_vote() -> None:
    assert format_phase_id(Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0)) == "DAY_1_VOTE"


def test_format_phase_id_night_mafia_discussion() -> None:
    assert (
        format_phase_id(Phase(kind=PhaseKind.NIGHT_MAFIA_DISCUSSION, day=1, round=0))
        == "NIGHT_1_MAFIA_DISCUSSION"
    )


def test_format_phase_id_night_actions() -> None:
    assert format_phase_id(Phase(kind=PhaseKind.NIGHT_ACTIONS, day=2, round=0)) == "NIGHT_2_ACTIONS"


def test_format_phase_id_terminal() -> None:
    assert format_phase_id(Phase(kind=PhaseKind.TERMINAL, day=5, round=0)) == "TERMINAL"


# --------------------------------------------------------------------------- #
# Basic structure
# --------------------------------------------------------------------------- #


def test_observation_has_all_required_top_level_fields() -> None:
    state = _state(Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=1))
    obs = build_observation(state, state.seats[4], EventLog(), mini7_v1)

    assert obs.ruleset_id == "mini7_v1"
    assert obs.game_public_id == "G-obs-test"
    assert obs.phase == "DAY_1_DISCUSSION_ROUND_1"
    assert obs.day == 1
    assert obs.round == 1
    assert obs.you.player_id == "P05"
    assert obs.you.alive is True
    assert obs.you.role is Role.VILLAGER
    assert obs.you.faction is Faction.TOWN
    assert obs.alive_players == ("P01", "P02", "P03", "P04", "P05", "P06", "P07")
    assert obs.dead_players == ()
    assert obs.your_private_memory == ""
    assert obs.message_limits.public_message_max_chars == mini7_v1.PUBLIC_MESSAGE_MAX_CHARS
    assert obs.message_limits.private_message_max_chars == mini7_v1.PRIVATE_MESSAGE_MAX_CHARS
    assert obs.message_limits.memory_update_max_chars == mini7_v1.MEMORY_UPDATE_MAX_CHARS


def test_observation_is_frozen() -> None:
    from pydantic import ValidationError

    state = _state(Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=1))
    obs = build_observation(state, state.seats[4], EventLog(), mini7_v1)
    with pytest.raises(ValidationError):
        obs.your_private_memory = "tampered"  # type: ignore[misc]


def test_observation_passes_through_private_memory() -> None:
    state = _state(Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=1))
    obs = build_observation(
        state, state.seats[4], EventLog(), mini7_v1, private_memory="prev notes"
    )
    assert obs.your_private_memory == "prev notes"


# --------------------------------------------------------------------------- #
# Role-conditional fields
# --------------------------------------------------------------------------- #


def test_villager_has_no_mafia_or_role_specific_fields() -> None:
    state = _state(Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=1))
    obs = build_observation(state, state.seats[4], EventLog(), mini7_v1)
    assert obs.mafia_teammates is None
    assert obs.previous_protected_target is None
    assert obs.inspection_history is None


def test_mafia_sees_other_mafia_teammates() -> None:
    state = _state(Phase(kind=PhaseKind.NIGHT_0_MAFIA_INTRO, day=0, round=0))
    obs_p01 = build_observation(state, state.seats[0], EventLog(), mini7_v1)
    assert obs_p01.mafia_teammates == ("P02",)
    obs_p02 = build_observation(state, state.seats[1], EventLog(), mini7_v1)
    assert obs_p02.mafia_teammates == ("P01",)


def test_doctor_sees_previous_protected_target() -> None:
    seats = _seven_seats(last_protected_target="P05")
    state = _state(Phase(kind=PhaseKind.NIGHT_ACTIONS, day=2, round=0), seats=seats)
    obs = build_observation(state, seats[3], EventLog(), mini7_v1)
    assert obs.previous_protected_target == "P05"
    assert obs.inspection_history is None
    assert obs.mafia_teammates is None


def test_doctor_first_night_protected_is_none() -> None:
    state = _state(Phase(kind=PhaseKind.NIGHT_ACTIONS, day=1, round=0))
    obs = build_observation(state, state.seats[3], EventLog(), mini7_v1)
    assert obs.previous_protected_target is None


def test_detective_has_empty_inspection_history_before_any_results() -> None:
    state = _state(Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=1))
    obs = build_observation(state, state.seats[2], EventLog(), mini7_v1)
    assert obs.inspection_history == ()
    assert obs.mafia_teammates is None
    assert obs.previous_protected_target is None


def test_detective_inspection_history_lists_only_own_results() -> None:
    state = _state(Phase(kind=PhaseKind.DAY_DISCUSSION, day=2, round=1))
    log = EventLog()
    _append(
        log,
        DetectiveResultDelivered(
            sequence=0,
            phase="DAY_2_DISCUSSION_ROUND_1",
            actor_player_id="P03",
            payload=DetectiveResultDeliveredPayload(target="P01", finding="MAFIA"),
        ),
    )
    # Another detective's result must not leak — synthesised via a different actor id.
    _append(
        log,
        DetectiveResultDelivered(
            sequence=1,
            phase="DAY_2_DISCUSSION_ROUND_1",
            actor_player_id="P99",
            payload=DetectiveResultDeliveredPayload(target="P02", finding="TOWN"),
        ),
    )
    obs = build_observation(state, state.seats[2], log, mini7_v1)
    assert obs.inspection_history is not None
    assert len(obs.inspection_history) == 1
    entry = obs.inspection_history[0]
    assert entry.target == "P01"
    assert entry.finding == "MAFIA"
    assert entry.phase == "DAY_2_DISCUSSION_ROUND_1"


# --------------------------------------------------------------------------- #
# Public events
# --------------------------------------------------------------------------- #


def test_public_events_include_public_visibility_only() -> None:
    state = _state(Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=2))
    log = EventLog()
    _append(
        log,
        PublicMessageSubmitted(
            sequence=0,
            phase="DAY_1_DISCUSSION_ROUND_1",
            actor_player_id="P05",
            payload=PublicMessageSubmittedPayload(text="hello", round_index=1),
        ),
    )
    _append(
        log,
        VoteSubmitted(
            sequence=1,
            phase="DAY_1_VOTE",
            actor_player_id="P05",
            payload=VoteSubmittedPayload(target="P01", is_abstain=False),
        ),
    )
    # SYSTEM event must not appear.
    _append(
        log,
        PhaseStarted(
            sequence=2,
            phase="DAY_1_DISCUSSION_ROUND_2",
            payload=PhaseStartedPayload(phase_kind="DAY_DISCUSSION", day=1, round=2),
        ),
    )
    obs = build_observation(state, state.seats[4], log, mini7_v1)
    event_types = [e.event_type for e in obs.public_events]
    assert event_types == ["PublicMessageSubmitted", "VoteSubmitted"]


def test_public_events_capped_at_recent_limit() -> None:
    state = _state(Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=1))
    log = EventLog()
    limit = mini7_v1.PUBLIC_TRANSCRIPT_RECENT_MESSAGE_LIMIT
    for i in range(limit + 5):
        _append(
            log,
            PublicMessageSubmitted(
                sequence=i,
                phase="DAY_1_DISCUSSION_ROUND_1",
                actor_player_id="P05",
                payload=PublicMessageSubmittedPayload(text=f"msg-{i}", round_index=1),
            ),
        )
    obs = build_observation(state, state.seats[4], log, mini7_v1)
    assert len(obs.public_events) == limit
    # Latest messages are kept; oldest are dropped.
    assert obs.public_events[0].payload["text"] == "msg-5"
    assert obs.public_events[-1].payload["text"] == f"msg-{limit + 4}"


# --------------------------------------------------------------------------- #
# Private events — privacy firewall
# --------------------------------------------------------------------------- #


def test_town_never_sees_mafia_private_chat() -> None:
    state = _state(Phase(kind=PhaseKind.NIGHT_MAFIA_DISCUSSION, day=1, round=0))
    log = EventLog()
    _append(
        log,
        PrivateMessageSubmitted(
            sequence=0,
            phase="NIGHT_1_MAFIA_DISCUSSION",
            actor_player_id="P01",
            payload=PrivateMessageSubmittedPayload(
                text="let's kill P05",
                channel_id="mafia",
            ),
        ),
    )
    _append(
        log,
        MafiaKillVoteSubmitted(
            sequence=1,
            phase="NIGHT_1_ACTIONS",
            actor_player_id="P02",
            payload=MafiaKillVoteSubmittedPayload(target="P05"),
        ),
    )
    for town_idx in (2, 3, 4, 5, 6):
        obs = build_observation(state, state.seats[town_idx], log, mini7_v1)
        assert obs.private_events == ()
        assert obs.mafia_teammates is None


def test_mafia_sees_mafia_private_chat_and_teammate_actions() -> None:
    state = _state(Phase(kind=PhaseKind.NIGHT_MAFIA_DISCUSSION, day=1, round=0))
    log = EventLog()
    _append(
        log,
        PrivateMessageSubmitted(
            sequence=0,
            phase="NIGHT_1_MAFIA_DISCUSSION",
            actor_player_id="P01",
            payload=PrivateMessageSubmittedPayload(text="kill P05", channel_id="mafia"),
        ),
    )
    _append(
        log,
        MafiaKillVoteSubmitted(
            sequence=1,
            phase="NIGHT_1_ACTIONS",
            actor_player_id="P02",
            payload=MafiaKillVoteSubmittedPayload(target="P05"),
        ),
    )
    obs = build_observation(state, state.seats[0], log, mini7_v1)
    seqs = [e.sequence for e in obs.private_events]
    assert seqs == [0, 1]


def test_seat_sees_own_private_action_events() -> None:
    state = _state(Phase(kind=PhaseKind.NIGHT_ACTIONS, day=1, round=0))
    log = EventLog()
    _append(
        log,
        ProtectSubmitted(
            sequence=0,
            phase="NIGHT_1_ACTIONS",
            actor_player_id="P04",
            payload=ProtectSubmittedPayload(target="P05"),
        ),
    )
    obs_doctor = build_observation(state, state.seats[3], log, mini7_v1)
    assert [e.sequence for e in obs_doctor.private_events] == [0]
    # Detective should not see doctor's private submission.
    obs_det = build_observation(state, state.seats[2], log, mini7_v1)
    assert obs_det.private_events == ()


def test_detective_sees_own_inspection_results() -> None:
    state = _state(Phase(kind=PhaseKind.DAY_DISCUSSION, day=2, round=1))
    log = EventLog()
    _append(
        log,
        DetectiveResultDelivered(
            sequence=0,
            phase="DAY_2_DISCUSSION_ROUND_1",
            actor_player_id="P03",
            payload=DetectiveResultDeliveredPayload(target="P01", finding="MAFIA"),
        ),
    )
    obs = build_observation(state, state.seats[2], log, mini7_v1)
    assert [e.sequence for e in obs.private_events] == [0]
    # Mafia should not see the detective's delivery even though it's PRIVATE.
    obs_mafia = build_observation(state, state.seats[0], log, mini7_v1)
    assert obs_mafia.private_events == ()


def test_role_feedback_lists_only_own_structured_public_id_results() -> None:
    state = _state(Phase(kind=PhaseKind.DAY_DISCUSSION, day=2, round=1))
    log = EventLog()
    _append(
        log,
        NightFeedbackDelivered(
            sequence=0,
            phase="NIGHT_1_ACTIONS",
            actor_player_id="P03",
            payload=NightFeedbackDeliveredPayload(
                code="TRACK_RESULT",
                target="P01",
                visited_player_ids=("P05", "P06"),
            ),
        ),
    )
    _append(
        log,
        NightFeedbackDelivered(
            sequence=1,
            phase="NIGHT_1_ACTIONS",
            actor_player_id="P04",
            payload=NightFeedbackDeliveredPayload(
                code="ACTION_BLOCKED",
                target="P05",
            ),
        ),
    )

    obs = build_observation(state, state.seats[2], log, mini7_v1)
    assert [entry.model_dump(mode="json") for entry in obs.role_feedback] == [
        {
            "code": "TRACK_RESULT",
            "phase": "NIGHT_1_ACTIONS",
            "target": "P01",
            "finding": None,
            "visited_player_ids": ["P05", "P06"],
            "visitor_player_ids": [],
        }
    ]
    assert [event.sequence for event in obs.private_events] == [0]

    other = build_observation(state, state.seats[4], log, mini7_v1)
    assert other.role_feedback == ()
    assert other.private_events == ()


# --------------------------------------------------------------------------- #
# Dead players, alive players, legal actions
# --------------------------------------------------------------------------- #


def test_dead_players_derived_from_player_eliminated_events() -> None:
    seats = _seven_seats(dead=("P02",), death_phase="DAY_1_VOTE")
    state = _state(Phase(kind=PhaseKind.NIGHT_MAFIA_DISCUSSION, day=1, round=0), seats=seats)
    log = EventLog()
    _append(
        log,
        PlayerEliminated(
            sequence=0,
            phase="DAY_1_VOTE",
            payload=PlayerEliminatedPayload(
                public_player_id="P02",
                role=Role.MAFIA_GOON,
                faction=Faction.MAFIA,
                cause="DAY_VOTE",
            ),
        ),
    )
    obs = build_observation(state, seats[4], log, mini7_v1)
    assert obs.alive_players == ("P01", "P03", "P04", "P05", "P06", "P07")
    assert len(obs.dead_players) == 1
    dead = obs.dead_players[0]
    assert dead.player_id == "P02"
    assert dead.day_or_night == "DAY_1_VOTE"
    assert dead.cause == "DAY_VOTE"


def test_legal_actions_match_per_phase_dispatch() -> None:
    state = _state(Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0))
    obs = build_observation(state, state.seats[4], EventLog(), mini7_v1)
    assert obs.legal_actions.allowed_action_types == [ActionType.VOTE, ActionType.ABSTAIN]
    assert set(obs.legal_actions.legal_targets) == {"P01", "P02", "P03", "P04", "P06", "P07"}


def test_legal_actions_dead_seat_empty() -> None:
    seats = _seven_seats(dead=("P05",))
    state = _state(Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0), seats=seats)
    obs = build_observation(state, seats[4], EventLog(), mini7_v1)
    assert obs.legal_actions.allowed_action_types == []
    assert obs.legal_actions.legal_targets == []


# --------------------------------------------------------------------------- #
# Model-level contract
# --------------------------------------------------------------------------- #


def test_observation_returns_pydantic_model() -> None:
    state = _state(Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=1))
    obs = build_observation(state, state.seats[4], EventLog(), mini7_v1)
    assert isinstance(obs, Observation)


def test_observations_module_has_no_forbidden_imports() -> None:
    src = Path("src/padrino/core/observations.py").read_text()
    tree = ast.parse(src)
    forbidden = {
        "padrino.db",
        "padrino.llm",
        "padrino.api",
        "padrino.runner",
        "sqlalchemy",
        "litellm",
        "httpx",
        "time",
        "datetime",
        "random",
        "secrets",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in forbidden, f"forbidden import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            assert node.module not in forbidden, f"forbidden from-import: {node.module}"
