"""Tests for the frozen Pydantic game-state models."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from padrino.core.engine.state import GameState, Phase, QueuedInspection, Seat
from padrino.core.enums import Faction, PhaseKind, Role


def _seat(
    pid: str,
    idx: int,
    role: Role,
    faction: Faction,
    *,
    alive: bool = True,
    death_phase: str | None = None,
    last_protected_target: str | None = None,
    queued_inspection_result: QueuedInspection | None = None,
) -> Seat:
    return Seat(
        public_player_id=pid,
        seat_index=idx,
        role=role,
        faction=faction,
        alive=alive,
        death_phase=death_phase,
        last_protected_target=last_protected_target,
        queued_inspection_result=queued_inspection_result,
    )


def _state(seats: tuple[Seat, ...]) -> GameState:
    return GameState(
        ruleset_id="mini7_v1",
        game_id="G-TEST",
        game_seed="seed-abc",
        current_phase=Phase(kind=PhaseKind.SETUP, day=0, round=0),
        seats=seats,
        day=0,
    )


SEATS_DEFAULT: tuple[Seat, ...] = (
    _seat("P01", 0, Role.MAFIA_GOON, Faction.MAFIA),
    _seat("P02", 1, Role.MAFIA_GOON, Faction.MAFIA),
    _seat("P03", 2, Role.DETECTIVE, Faction.TOWN),
    _seat("P04", 3, Role.DOCTOR, Faction.TOWN),
    _seat("P05", 4, Role.VILLAGER, Faction.TOWN),
    _seat("P06", 5, Role.VILLAGER, Faction.TOWN),
    _seat("P07", 6, Role.VILLAGER, Faction.TOWN),
)


def test_seat_is_frozen() -> None:
    seat = SEATS_DEFAULT[0]
    with pytest.raises(ValidationError):
        seat.alive = False  # type: ignore[misc]


def test_phase_is_frozen() -> None:
    phase = Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0)
    with pytest.raises(ValidationError):
        phase.day = 2  # type: ignore[misc]


def test_game_state_is_frozen() -> None:
    state = _state(SEATS_DEFAULT)
    with pytest.raises(ValidationError):
        state.day = 5  # type: ignore[misc]


def test_queued_inspection_is_frozen() -> None:
    q = QueuedInspection(target="P03", finding="MAFIA")
    with pytest.raises(ValidationError):
        q.target = "P04"  # type: ignore[misc]


def test_queued_inspection_finding_literal_enforced() -> None:
    with pytest.raises(ValidationError):
        QueuedInspection(target="P03", finding="UNKNOWN")  # type: ignore[arg-type]


def test_living_seats_returns_only_alive_in_order() -> None:
    dead = SEATS_DEFAULT[2].model_copy(update={"alive": False, "death_phase": "DAY_1_VOTE"})
    seats = (*SEATS_DEFAULT[:2], dead, *SEATS_DEFAULT[3:])
    state = _state(seats)
    living = state.living_seats()
    assert [s.public_player_id for s in living] == ["P01", "P02", "P04", "P05", "P06", "P07"]


def test_living_seats_by_faction_filters_alive_and_faction() -> None:
    dead_mafia = SEATS_DEFAULT[0].model_copy(update={"alive": False})
    seats = (dead_mafia, *SEATS_DEFAULT[1:])
    state = _state(seats)
    mafia = state.living_seats_by_faction(Faction.MAFIA)
    assert [s.public_player_id for s in mafia] == ["P02"]
    town = state.living_seats_by_faction(Faction.TOWN)
    assert [s.public_player_id for s in town] == ["P03", "P04", "P05", "P06", "P07"]


def test_seat_by_public_id_returns_seat_or_none() -> None:
    state = _state(SEATS_DEFAULT)
    assert state.seat_by_public_id("P03") is SEATS_DEFAULT[2]
    assert state.seat_by_public_id("P99") is None


def test_alive_count_by_faction_counts_only_alive() -> None:
    state = _state(SEATS_DEFAULT)
    assert state.alive_count_by_faction(Faction.MAFIA) == 2
    assert state.alive_count_by_faction(Faction.TOWN) == 5

    dead_mafia = SEATS_DEFAULT[0].model_copy(update={"alive": False})
    dead_town = SEATS_DEFAULT[2].model_copy(update={"alive": False})
    seats = (dead_mafia, SEATS_DEFAULT[1], dead_town, *SEATS_DEFAULT[3:])
    state2 = _state(seats)
    assert state2.alive_count_by_faction(Faction.MAFIA) == 1
    assert state2.alive_count_by_faction(Faction.TOWN) == 4


def test_seat_defaults_are_none() -> None:
    seat = Seat(
        public_player_id="P01",
        seat_index=0,
        role=Role.VILLAGER,
        faction=Faction.TOWN,
        alive=True,
    )
    assert seat.death_phase is None
    assert seat.last_protected_target is None
    assert seat.queued_inspection_result is None


def test_seats_field_accepts_tuple_and_preserves_order() -> None:
    state = _state(SEATS_DEFAULT)
    assert isinstance(state.seats, tuple)
    assert [s.seat_index for s in state.seats] == [0, 1, 2, 3, 4, 5, 6]


def test_game_state_terminal_fields_default_none() -> None:
    state = _state(SEATS_DEFAULT)
    assert state.terminal_result is None
    assert state.terminal_reason is None
