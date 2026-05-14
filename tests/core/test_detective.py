"""Tests for the detective night investigate resolver."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from padrino.core.engine.actions import Action
from padrino.core.engine.resolvers.detective import (
    FINDING_MAFIA,
    FINDING_TOWN,
    REASON_DEAD_DETECTIVE,
    REASON_INVALID_TARGET,
    REASON_NO_DETECTIVE,
    REASON_NO_SUBMISSION,
    REASON_RESOLVED,
    REASON_SELF_TARGET,
    DetectiveResult,
    resolve_detective_investigation,
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
) -> Seat:
    return Seat(
        public_player_id=pid,
        seat_index=idx,
        role=role,
        faction=faction,
        alive=alive,
    )


def _seats(
    *,
    detective_alive: bool = True,
    p01_alive: bool = True,
    p05_alive: bool = True,
) -> tuple[Seat, ...]:
    return (
        _seat("P01", 0, Role.MAFIA_GOON, Faction.MAFIA, alive=p01_alive),
        _seat("P02", 1, Role.MAFIA_GOON, Faction.MAFIA),
        _seat("P03", 2, Role.DETECTIVE, Faction.TOWN, alive=detective_alive),
        _seat("P04", 3, Role.DOCTOR, Faction.TOWN),
        _seat("P05", 4, Role.VILLAGER, Faction.TOWN, alive=p05_alive),
        _seat("P06", 5, Role.VILLAGER, Faction.TOWN),
        _seat("P07", 6, Role.VILLAGER, Faction.TOWN),
    )


def _state(seats: tuple[Seat, ...]) -> GameState:
    return GameState(
        ruleset_id="mini7_v1",
        game_id="G-TEST",
        game_seed="seed-detective",
        current_phase=Phase(kind=PhaseKind.NIGHT_ACTIONS, day=1, round=0),
        seats=seats,
        day=1,
    )


def _investigate(target: str | None) -> Action:
    return Action(type=ActionType.INVESTIGATE, target=target)


def test_investigate_town_target_returns_town() -> None:
    state = _state(_seats())
    result = resolve_detective_investigation(state, _investigate("P05"))
    assert isinstance(result, DetectiveResult)
    assert result.target == "P05"
    assert result.finding == FINDING_TOWN
    assert result.reason == REASON_RESOLVED


def test_investigate_mafia_target_returns_mafia() -> None:
    state = _state(_seats())
    result = resolve_detective_investigation(state, _investigate("P01"))
    assert result.target == "P01"
    assert result.finding == FINDING_MAFIA
    assert result.reason == REASON_RESOLVED


def test_investigate_self_returns_none() -> None:
    state = _state(_seats())
    result = resolve_detective_investigation(state, _investigate("P03"))
    assert result.target is None
    assert result.finding is None
    assert result.reason == REASON_SELF_TARGET


def test_investigate_dead_target_returns_none() -> None:
    state = _state(_seats(p05_alive=False))
    result = resolve_detective_investigation(state, _investigate("P05"))
    assert result.target is None
    assert result.finding is None
    assert result.reason == REASON_INVALID_TARGET


def test_investigate_dead_mafia_target_returns_none() -> None:
    state = _state(_seats(p01_alive=False))
    result = resolve_detective_investigation(state, _investigate("P01"))
    assert result.target is None
    assert result.finding is None
    assert result.reason == REASON_INVALID_TARGET


def test_no_submission_returns_none() -> None:
    state = _state(_seats())
    result = resolve_detective_investigation(state, None)
    assert result.target is None
    assert result.finding is None
    assert result.reason == REASON_NO_SUBMISSION


def test_dead_detective_returns_none() -> None:
    state = _state(_seats(detective_alive=False))
    result = resolve_detective_investigation(state, _investigate("P01"))
    assert result.target is None
    assert result.finding is None
    assert result.reason == REASON_DEAD_DETECTIVE


def test_nonexistent_target_returns_none() -> None:
    state = _state(_seats())
    result = resolve_detective_investigation(state, _investigate("P99"))
    assert result.target is None
    assert result.finding is None
    assert result.reason == REASON_INVALID_TARGET


def test_investigate_with_no_target_returns_none() -> None:
    state = _state(_seats())
    result = resolve_detective_investigation(
        state, Action(type=ActionType.INVESTIGATE, target=None)
    )
    assert result.target is None
    assert result.finding is None
    assert result.reason == REASON_INVALID_TARGET


def test_non_investigate_action_returns_none() -> None:
    state = _state(_seats())
    result = resolve_detective_investigation(state, Action(type=ActionType.NOOP))
    assert result.target is None
    assert result.finding is None
    assert result.reason == REASON_INVALID_TARGET


def test_no_detective_in_state_returns_none() -> None:
    seats = (
        _seat("P01", 0, Role.MAFIA_GOON, Faction.MAFIA),
        _seat("P02", 1, Role.MAFIA_GOON, Faction.MAFIA),
        _seat("P03", 2, Role.VILLAGER, Faction.TOWN),
        _seat("P04", 3, Role.DOCTOR, Faction.TOWN),
        _seat("P05", 4, Role.VILLAGER, Faction.TOWN),
        _seat("P06", 5, Role.VILLAGER, Faction.TOWN),
        _seat("P07", 6, Role.VILLAGER, Faction.TOWN),
    )
    state = _state(seats)
    result = resolve_detective_investigation(state, _investigate("P01"))
    assert result.target is None
    assert result.finding is None
    assert result.reason == REASON_NO_DETECTIVE


def test_chat_field_is_never_read() -> None:
    fields = set(Action.model_fields.keys())
    assert fields == {"type", "target"}


def test_result_is_immutable() -> None:
    state = _state(_seats())
    result = resolve_detective_investigation(state, _investigate("P01"))
    with pytest.raises(ValidationError):
        result.finding = "TOWN"  # type: ignore[misc]
