"""Tests for the win-condition checker."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from padrino.core.engine.state import GameState, Phase, Seat
from padrino.core.engine.win_conditions import (
    REASON_ALL_MAFIA_ELIMINATED,
    REASON_MAX_DAYS_REACHED,
    REASON_PARITY_REACHED,
    WinResult,
    check_win,
)
from padrino.core.enums import Faction, PhaseKind, Role
from padrino.core.rulesets import mini7_v1


def _seat(
    pid: str,
    idx: int,
    role: Role,
    faction: Faction,
    *,
    alive: bool = True,
) -> Seat:
    return Seat(
        public_player_id=pid,
        seat_index=idx,
        role=role,
        faction=faction,
        alive=alive,
    )


def _state(
    seats: tuple[Seat, ...],
    *,
    day: int = 1,
    phase: Phase | None = None,
    terminal_result: str | None = None,
    terminal_reason: str | None = None,
) -> GameState:
    return GameState(
        ruleset_id="mini7_v1",
        game_id="G-TEST",
        game_seed="seed-abc",
        current_phase=phase or Phase(kind=PhaseKind.DAY_VOTE, day=day, round=0),
        seats=seats,
        day=day,
        terminal_result=terminal_result,
        terminal_reason=terminal_reason,
    )


def _full_mini7_seats(
    *,
    mafia_alive: int = 2,
    town_alive: int = 5,
) -> tuple[Seat, ...]:
    """Build a 7-seat mini7 lineup with the requested alive counts.

    Order: P01-P02 mafia, P03 detective, P04 doctor, P05-P07 villagers.
    The first `mafia_alive` mafia and `town_alive` town seats are alive.
    """
    seats: list[Seat] = []
    mafia_template = [
        (Role.MAFIA_GOON, Faction.MAFIA),
        (Role.MAFIA_GOON, Faction.MAFIA),
    ]
    town_template = [
        (Role.DETECTIVE, Faction.TOWN),
        (Role.DOCTOR, Faction.TOWN),
        (Role.VILLAGER, Faction.TOWN),
        (Role.VILLAGER, Faction.TOWN),
        (Role.VILLAGER, Faction.TOWN),
    ]
    pid_idx = 0
    for i, (role, faction) in enumerate(mafia_template):
        seats.append(
            _seat(
                f"P{pid_idx + 1:02d}",
                pid_idx,
                role,
                faction,
                alive=i < mafia_alive,
            )
        )
        pid_idx += 1
    for i, (role, faction) in enumerate(town_template):
        seats.append(
            _seat(
                f"P{pid_idx + 1:02d}",
                pid_idx,
                role,
                faction,
                alive=i < town_alive,
            )
        )
        pid_idx += 1
    return tuple(seats)


def test_last_mafia_eliminated_returns_town_win() -> None:
    seats = _full_mini7_seats(mafia_alive=0, town_alive=4)
    result = check_win(_state(seats, day=2), mini7_v1)
    assert result == WinResult(winner="TOWN", reason=REASON_ALL_MAFIA_ELIMINATED)


def test_town_win_when_all_mafia_dead_even_with_few_town() -> None:
    seats = _full_mini7_seats(mafia_alive=0, town_alive=1)
    result = check_win(_state(seats, day=3), mini7_v1)
    assert result is not None
    assert result.winner == "TOWN"


def test_one_mafia_versus_one_town_parity_is_mafia_win() -> None:
    seats = _full_mini7_seats(mafia_alive=1, town_alive=1)
    result = check_win(_state(seats, day=3), mini7_v1)
    assert result == WinResult(winner="MAFIA", reason=REASON_PARITY_REACHED)


def test_mafia_outnumbering_town_is_mafia_win() -> None:
    seats = _full_mini7_seats(mafia_alive=2, town_alive=1)
    result = check_win(_state(seats, day=3), mini7_v1)
    assert result is not None
    assert result.winner == "MAFIA"
    assert result.reason == REASON_PARITY_REACHED


def test_two_mafia_versus_three_town_returns_none() -> None:
    seats = _full_mini7_seats(mafia_alive=2, town_alive=3)
    result = check_win(_state(seats, day=2), mini7_v1)
    assert result is None


def test_two_mafia_versus_five_town_initial_state_returns_none() -> None:
    seats = _full_mini7_seats(mafia_alive=2, town_alive=5)
    result = check_win(_state(seats, day=1), mini7_v1)
    assert result is None


def test_max_days_reached_with_no_winner_returns_draw() -> None:
    seats = _full_mini7_seats(mafia_alive=1, town_alive=2)
    state = _state(
        seats,
        day=mini7_v1.MAX_DAYS + 1,
        phase=Phase(kind=PhaseKind.TERMINAL, day=mini7_v1.MAX_DAYS + 1, round=0),
    )
    result = check_win(state, mini7_v1)
    assert result == WinResult(winner="DRAW", reason=REASON_MAX_DAYS_REACHED)


def test_max_days_exact_threshold_returns_none() -> None:
    seats = _full_mini7_seats(mafia_alive=1, town_alive=2)
    result = check_win(_state(seats, day=mini7_v1.MAX_DAYS), mini7_v1)
    assert result is None


def test_town_win_takes_priority_over_max_days() -> None:
    seats = _full_mini7_seats(mafia_alive=0, town_alive=2)
    state = _state(seats, day=mini7_v1.MAX_DAYS + 2)
    result = check_win(state, mini7_v1)
    assert result is not None
    assert result.winner == "TOWN"


def test_mafia_win_takes_priority_over_max_days() -> None:
    seats = _full_mini7_seats(mafia_alive=1, town_alive=1)
    state = _state(seats, day=mini7_v1.MAX_DAYS + 2)
    result = check_win(state, mini7_v1)
    assert result is not None
    assert result.winner == "MAFIA"


def test_idempotent_after_terminal_state_set() -> None:
    seats = _full_mini7_seats(mafia_alive=0, town_alive=3)
    state = _state(
        seats,
        day=4,
        terminal_result="TOWN",
        terminal_reason=REASON_ALL_MAFIA_ELIMINATED,
    )
    first = check_win(state, mini7_v1)
    second = check_win(state, mini7_v1)
    assert first == second
    assert first == WinResult(winner="TOWN", reason=REASON_ALL_MAFIA_ELIMINATED)


def test_check_win_does_not_mutate_state() -> None:
    seats = _full_mini7_seats(mafia_alive=1, town_alive=2)
    state = _state(seats, day=2)
    snapshot = state.model_copy()
    check_win(state, mini7_v1)
    assert state == snapshot


def test_win_result_is_frozen() -> None:
    result = WinResult(winner="TOWN", reason=REASON_ALL_MAFIA_ELIMINATED)
    with pytest.raises(ValidationError):
        result.winner = "MAFIA"  # type: ignore[misc]


def test_zero_alive_anyone_returns_town_win() -> None:
    seats = _full_mini7_seats(mafia_alive=0, town_alive=0)
    result = check_win(_state(seats, day=3), mini7_v1)
    assert result is not None
    assert result.winner == "TOWN"
