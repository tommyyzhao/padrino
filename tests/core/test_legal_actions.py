"""Tests for `legal_actions_for` — single source of truth for legal moves."""

from __future__ import annotations

import pytest

from padrino.core.engine.legal_actions import LegalActions, legal_actions_for
from padrino.core.engine.state import GameState, Phase, Seat
from padrino.core.enums import ActionType, Faction, PhaseKind, Role


def _seat(
    pid: str,
    idx: int,
    role: Role,
    faction: Faction,
    *,
    alive: bool = True,
    last_protected_target: str | None = None,
) -> Seat:
    return Seat(
        public_player_id=pid,
        seat_index=idx,
        role=role,
        faction=faction,
        alive=alive,
        last_protected_target=last_protected_target,
    )


SEATS: tuple[Seat, ...] = (
    _seat("P01", 0, Role.MAFIA_GOON, Faction.MAFIA),
    _seat("P02", 1, Role.MAFIA_GOON, Faction.MAFIA),
    _seat("P03", 2, Role.DETECTIVE, Faction.TOWN),
    _seat("P04", 3, Role.DOCTOR, Faction.TOWN),
    _seat("P05", 4, Role.VILLAGER, Faction.TOWN),
    _seat("P06", 5, Role.VILLAGER, Faction.TOWN),
    _seat("P07", 6, Role.VILLAGER, Faction.TOWN),
)


def _state(phase: Phase, seats: tuple[Seat, ...] = SEATS) -> GameState:
    return GameState(
        ruleset_id="mini7_v1",
        game_id="G-TEST",
        game_seed="seed-abc",
        current_phase=phase,
        seats=seats,
        day=phase.day,
    )


ALL_ROLES: list[Role] = [
    Role.MAFIA_GOON,
    Role.DETECTIVE,
    Role.DOCTOR,
    Role.VILLAGER,
]


# --- Dead seats always return empty ----------------------------------------


@pytest.mark.parametrize(
    "phase",
    [
        Phase(kind=PhaseKind.SETUP, day=0, round=0),
        Phase(kind=PhaseKind.NIGHT_0_MAFIA_INTRO, day=0, round=0),
        Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=1),
        Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0),
        Phase(kind=PhaseKind.NIGHT_MAFIA_DISCUSSION, day=1, round=0),
        Phase(kind=PhaseKind.NIGHT_ACTIONS, day=1, round=0),
        Phase(kind=PhaseKind.TERMINAL, day=5, round=0),
    ],
)
@pytest.mark.parametrize("role", ALL_ROLES)
def test_dead_seats_always_empty(phase: Phase, role: Role) -> None:
    faction = Faction.MAFIA if role is Role.MAFIA_GOON else Faction.TOWN
    dead = _seat("P01", 0, role, faction, alive=False)
    seats = (dead, *SEATS[1:])
    legal = legal_actions_for(_state(phase, seats), dead)
    assert legal.allowed_action_types == []
    assert legal.legal_targets == []


# --- Discussion phases -----------------------------------------------------


@pytest.mark.parametrize("round_", [1, 2, 3])
@pytest.mark.parametrize("role", ALL_ROLES)
def test_discussion_phase_allows_only_noop_for_every_role(role: Role, round_: int) -> None:
    faction = Faction.MAFIA if role is Role.MAFIA_GOON else Faction.TOWN
    seat = _seat("P01", 0, role, faction)
    seats = (seat, *SEATS[1:])
    phase = Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=round_)
    legal = legal_actions_for(_state(phase, seats), seat)
    assert legal.allowed_action_types == [ActionType.NOOP]
    assert legal.legal_targets == []


# --- Day vote --------------------------------------------------------------


def test_day_vote_targets_are_living_others_plus_abstain() -> None:
    phase = Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0)
    voter = SEATS[2]
    legal = legal_actions_for(_state(phase), voter)
    assert legal.allowed_action_types == [ActionType.VOTE, ActionType.ABSTAIN]
    assert legal.legal_targets == ["P01", "P02", "P04", "P05", "P06", "P07"]


def test_day_vote_excludes_dead_targets() -> None:
    dead = SEATS[4].model_copy(update={"alive": False, "death_phase": "NIGHT_1_ACTIONS"})
    seats = (*SEATS[:4], dead, *SEATS[5:])
    phase = Phase(kind=PhaseKind.DAY_VOTE, day=2, round=0)
    voter = seats[0]
    legal = legal_actions_for(_state(phase, seats), voter)
    assert legal.allowed_action_types == [ActionType.VOTE, ActionType.ABSTAIN]
    assert "P05" not in legal.legal_targets
    assert legal.legal_targets == ["P02", "P03", "P04", "P06", "P07"]


@pytest.mark.parametrize("role", ALL_ROLES)
def test_day_vote_same_for_every_role(role: Role) -> None:
    faction = Faction.MAFIA if role is Role.MAFIA_GOON else Faction.TOWN
    seat = _seat("P01", 0, role, faction)
    seats = (seat, *SEATS[1:])
    phase = Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0)
    legal = legal_actions_for(_state(phase, seats), seat)
    assert legal.allowed_action_types == [ActionType.VOTE, ActionType.ABSTAIN]
    assert legal.legal_targets == ["P02", "P03", "P04", "P05", "P06", "P07"]


# --- Mafia discussion (night intro + nightly) ------------------------------


@pytest.mark.parametrize(
    "phase",
    [
        Phase(kind=PhaseKind.NIGHT_0_MAFIA_INTRO, day=0, round=0),
        Phase(kind=PhaseKind.NIGHT_MAFIA_DISCUSSION, day=1, round=0),
        Phase(kind=PhaseKind.NIGHT_MAFIA_DISCUSSION, day=3, round=0),
    ],
)
def test_mafia_discussion_mafia_seats_allowed_noop(phase: Phase) -> None:
    mafia_seat = SEATS[0]
    legal = legal_actions_for(_state(phase), mafia_seat)
    assert legal.allowed_action_types == [ActionType.NOOP]
    assert legal.legal_targets == []


@pytest.mark.parametrize(
    "phase",
    [
        Phase(kind=PhaseKind.NIGHT_0_MAFIA_INTRO, day=0, round=0),
        Phase(kind=PhaseKind.NIGHT_MAFIA_DISCUSSION, day=1, round=0),
    ],
)
@pytest.mark.parametrize("town_index", [2, 3, 4])
def test_mafia_discussion_town_seats_have_no_legal_actions(phase: Phase, town_index: int) -> None:
    town_seat = SEATS[town_index]
    legal = legal_actions_for(_state(phase), town_seat)
    assert legal.allowed_action_types == []
    assert legal.legal_targets == []


# --- Night actions ---------------------------------------------------------


def test_night_actions_mafia_targets_living_non_mafia() -> None:
    phase = Phase(kind=PhaseKind.NIGHT_ACTIONS, day=1, round=0)
    mafia_seat = SEATS[0]
    legal = legal_actions_for(_state(phase), mafia_seat)
    assert legal.allowed_action_types == [ActionType.MAFIA_KILL]
    assert legal.legal_targets == ["P03", "P04", "P05", "P06", "P07"]


def test_night_actions_mafia_excludes_dead_targets() -> None:
    dead_villager = SEATS[5].model_copy(update={"alive": False})
    seats = (*SEATS[:5], dead_villager, SEATS[6])
    phase = Phase(kind=PhaseKind.NIGHT_ACTIONS, day=2, round=0)
    legal = legal_actions_for(_state(phase, seats), seats[1])
    assert legal.allowed_action_types == [ActionType.MAFIA_KILL]
    assert legal.legal_targets == ["P03", "P04", "P05", "P07"]


def test_night_actions_doctor_targets_include_self_when_no_prior_protect() -> None:
    phase = Phase(kind=PhaseKind.NIGHT_ACTIONS, day=1, round=0)
    doctor = SEATS[3]
    legal = legal_actions_for(_state(phase), doctor)
    assert legal.allowed_action_types == [ActionType.PROTECT]
    assert legal.legal_targets == ["P01", "P02", "P03", "P04", "P05", "P06", "P07"]


def test_night_actions_doctor_excludes_last_protected_target() -> None:
    doctor = SEATS[3].model_copy(update={"last_protected_target": "P05"})
    seats = (*SEATS[:3], doctor, *SEATS[4:])
    phase = Phase(kind=PhaseKind.NIGHT_ACTIONS, day=2, round=0)
    legal = legal_actions_for(_state(phase, seats), doctor)
    assert legal.allowed_action_types == [ActionType.PROTECT]
    assert "P05" not in legal.legal_targets
    assert legal.legal_targets == ["P01", "P02", "P03", "P04", "P06", "P07"]


def test_night_actions_doctor_may_repeat_self_if_self_was_not_last_target() -> None:
    doctor = SEATS[3].model_copy(update={"last_protected_target": "P01"})
    seats = (*SEATS[:3], doctor, *SEATS[4:])
    phase = Phase(kind=PhaseKind.NIGHT_ACTIONS, day=2, round=0)
    legal = legal_actions_for(_state(phase, seats), doctor)
    assert "P04" in legal.legal_targets
    assert "P01" not in legal.legal_targets


def test_night_actions_doctor_cannot_protect_self_consecutively() -> None:
    doctor = SEATS[3].model_copy(update={"last_protected_target": "P04"})
    seats = (*SEATS[:3], doctor, *SEATS[4:])
    phase = Phase(kind=PhaseKind.NIGHT_ACTIONS, day=2, round=0)
    legal = legal_actions_for(_state(phase, seats), doctor)
    assert "P04" not in legal.legal_targets
    assert legal.legal_targets == ["P01", "P02", "P03", "P05", "P06", "P07"]


def test_night_actions_doctor_excludes_dead_seats() -> None:
    dead = SEATS[4].model_copy(update={"alive": False})
    seats = (*SEATS[:4], dead, *SEATS[5:])
    phase = Phase(kind=PhaseKind.NIGHT_ACTIONS, day=2, round=0)
    legal = legal_actions_for(_state(phase, seats), seats[3])
    assert "P05" not in legal.legal_targets


def test_night_actions_detective_targets_living_others_including_mafia() -> None:
    phase = Phase(kind=PhaseKind.NIGHT_ACTIONS, day=1, round=0)
    detective = SEATS[2]
    legal = legal_actions_for(_state(phase), detective)
    assert legal.allowed_action_types == [ActionType.INVESTIGATE]
    assert legal.legal_targets == ["P01", "P02", "P04", "P05", "P06", "P07"]


def test_night_actions_villager_only_noop() -> None:
    phase = Phase(kind=PhaseKind.NIGHT_ACTIONS, day=1, round=0)
    villager = SEATS[4]
    legal = legal_actions_for(_state(phase), villager)
    assert legal.allowed_action_types == [ActionType.NOOP]
    assert legal.legal_targets == []


# --- SETUP / TERMINAL: no one acts -----------------------------------------


@pytest.mark.parametrize(
    "phase",
    [
        Phase(kind=PhaseKind.SETUP, day=0, round=0),
        Phase(kind=PhaseKind.TERMINAL, day=5, round=0),
    ],
)
@pytest.mark.parametrize("seat_index", [0, 2, 3, 4])
def test_setup_and_terminal_have_no_actions(phase: Phase, seat_index: int) -> None:
    legal = legal_actions_for(_state(phase), SEATS[seat_index])
    assert legal.allowed_action_types == []
    assert legal.legal_targets == []


# --- LegalActions model ----------------------------------------------------


def test_legal_actions_model_is_constructable() -> None:
    legal = LegalActions(
        allowed_action_types=[ActionType.VOTE, ActionType.ABSTAIN],
        legal_targets=["P01", "P02"],
    )
    assert legal.allowed_action_types == [ActionType.VOTE, ActionType.ABSTAIN]
    assert legal.legal_targets == ["P01", "P02"]
