"""Tests for the doctor night protect resolver."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from padrino.core.engine.actions import Action
from padrino.core.engine.resolvers.doctor_protect import (
    REASON_DEAD_DOCTOR,
    REASON_INVALID_TARGET,
    REASON_NO_DOCTOR,
    REASON_NO_SUBMISSION,
    REASON_PROTECTED,
    REASON_REPEAT_VIOLATION,
    DoctorProtectResult,
    resolve_doctor_protect,
)
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


def _seats(
    *,
    doctor_alive: bool = True,
    last_protected_target: str | None = None,
    p05_alive: bool = True,
) -> tuple[Seat, ...]:
    return (
        _seat("P01", 0, Role.MAFIA_GOON, Faction.MAFIA),
        _seat("P02", 1, Role.MAFIA_GOON, Faction.MAFIA),
        _seat("P03", 2, Role.DETECTIVE, Faction.TOWN),
        _seat(
            "P04",
            3,
            Role.DOCTOR,
            Faction.TOWN,
            alive=doctor_alive,
            last_protected_target=last_protected_target,
        ),
        _seat("P05", 4, Role.VILLAGER, Faction.TOWN, alive=p05_alive),
        _seat("P06", 5, Role.VILLAGER, Faction.TOWN),
        _seat("P07", 6, Role.VILLAGER, Faction.TOWN),
    )


def _state(seats: tuple[Seat, ...]) -> GameState:
    return GameState(
        ruleset_id="mini7_v1",
        game_id="G-TEST",
        game_seed="seed-doctor",
        current_phase=Phase(kind=PhaseKind.NIGHT_ACTIONS, day=1, round=0),
        seats=seats,
        day=1,
    )


def _protect(target: str | None) -> Action:
    return Action(type=ActionType.PROTECT, target=target)


def test_first_night_protect_any_target_succeeds() -> None:
    state = _state(_seats())
    result = resolve_doctor_protect(state, _protect("P05"))
    assert isinstance(result, DoctorProtectResult)
    assert result.protected == "P05"
    assert result.reason == REASON_PROTECTED


def test_first_night_self_protect_succeeds() -> None:
    state = _state(_seats())
    result = resolve_doctor_protect(state, _protect("P04"))
    assert result.protected == "P04"
    assert result.reason == REASON_PROTECTED


def test_repeat_target_coerced_to_noop() -> None:
    state = _state(_seats(last_protected_target="P05"))
    result = resolve_doctor_protect(state, _protect("P05"))
    assert result.protected is None
    assert result.reason == REASON_REPEAT_VIOLATION


def test_repeat_self_target_coerced_to_noop() -> None:
    state = _state(_seats(last_protected_target="P04"))
    result = resolve_doctor_protect(state, _protect("P04"))
    assert result.protected is None
    assert result.reason == REASON_REPEAT_VIOLATION


def test_alternating_self_and_other_allowed() -> None:
    # Previous night protected P05; tonight protect self → OK
    state_a = _state(_seats(last_protected_target="P05"))
    result_a = resolve_doctor_protect(state_a, _protect("P04"))
    assert result_a.protected == "P04"
    assert result_a.reason == REASON_PROTECTED

    # Previous night protected self; tonight protect P05 → OK
    state_b = _state(_seats(last_protected_target="P04"))
    result_b = resolve_doctor_protect(state_b, _protect("P05"))
    assert result_b.protected == "P05"
    assert result_b.reason == REASON_PROTECTED


def test_dead_doctor_returns_none() -> None:
    state = _state(_seats(doctor_alive=False))
    result = resolve_doctor_protect(state, _protect("P05"))
    assert result.protected is None
    assert result.reason == REASON_DEAD_DOCTOR


def test_missing_submission_returns_none() -> None:
    state = _state(_seats())
    result = resolve_doctor_protect(state, None)
    assert result.protected is None
    assert result.reason == REASON_NO_SUBMISSION


def test_dead_target_invalid() -> None:
    state = _state(_seats(p05_alive=False))
    result = resolve_doctor_protect(state, _protect("P05"))
    assert result.protected is None
    assert result.reason == REASON_INVALID_TARGET


def test_nonexistent_target_invalid() -> None:
    state = _state(_seats())
    result = resolve_doctor_protect(state, _protect("P99"))
    assert result.protected is None
    assert result.reason == REASON_INVALID_TARGET


def test_non_protect_action_invalid() -> None:
    state = _state(_seats())
    result = resolve_doctor_protect(state, Action(type=ActionType.NOOP))
    assert result.protected is None
    assert result.reason == REASON_INVALID_TARGET


def test_protect_with_no_target_invalid() -> None:
    state = _state(_seats())
    result = resolve_doctor_protect(state, Action(type=ActionType.PROTECT, target=None))
    assert result.protected is None
    assert result.reason == REASON_INVALID_TARGET


def test_no_doctor_in_state_returns_none() -> None:
    seats = (
        _seat("P01", 0, Role.MAFIA_GOON, Faction.MAFIA),
        _seat("P02", 1, Role.MAFIA_GOON, Faction.MAFIA),
        _seat("P03", 2, Role.DETECTIVE, Faction.TOWN),
        _seat("P04", 3, Role.VILLAGER, Faction.TOWN),
        _seat("P05", 4, Role.VILLAGER, Faction.TOWN),
        _seat("P06", 5, Role.VILLAGER, Faction.TOWN),
        _seat("P07", 6, Role.VILLAGER, Faction.TOWN),
    )
    state = _state(seats)
    result = resolve_doctor_protect(state, _protect("P05"))
    assert result.protected is None
    assert result.reason == REASON_NO_DOCTOR


def test_chat_field_is_never_read() -> None:
    fields = set(Action.model_fields.keys())
    assert fields == {"type", "target"}


def test_result_is_immutable() -> None:
    state = _state(_seats())
    result = resolve_doctor_protect(state, _protect("P05"))
    with pytest.raises(ValidationError):
        result.protected = "P99"  # type: ignore[misc]
