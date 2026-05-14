"""Tests for the mafia night kill resolver."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from padrino.core.engine.actions import Action
from padrino.core.engine.resolvers.mafia_kill import (
    REASON_ALL_INVALID,
    REASON_TIE,
    REASON_UNIQUE_PLURALITY,
    MafiaKillResult,
    resolve_mafia_kill,
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


def _all_living_seats() -> tuple[Seat, ...]:
    return (
        _seat("P01", 0, Role.MAFIA_GOON, Faction.MAFIA),
        _seat("P02", 1, Role.MAFIA_GOON, Faction.MAFIA),
        _seat("P03", 2, Role.DETECTIVE, Faction.TOWN),
        _seat("P04", 3, Role.DOCTOR, Faction.TOWN),
        _seat("P05", 4, Role.VILLAGER, Faction.TOWN),
        _seat("P06", 5, Role.VILLAGER, Faction.TOWN),
        _seat("P07", 6, Role.VILLAGER, Faction.TOWN),
    )


def _state(seats: tuple[Seat, ...]) -> GameState:
    return GameState(
        ruleset_id="mini7_v1",
        game_id="G-TEST",
        game_seed="seed-abc",
        current_phase=Phase(kind=PhaseKind.NIGHT_ACTIONS, day=1, round=0),
        seats=seats,
        day=1,
    )


def _kill(target: str) -> Action:
    return Action(type=ActionType.MAFIA_KILL, target=target)


def test_two_mafia_agree_on_target() -> None:
    state = _state(_all_living_seats())
    submissions = {
        "P01": _kill("P03"),
        "P02": _kill("P03"),
    }
    result = resolve_mafia_kill(state, submissions)
    assert isinstance(result, MafiaKillResult)
    assert result.target == "P03"
    assert result.vote_tally == {"P03": 2}
    assert result.reason == REASON_UNIQUE_PLURALITY


def test_tie_yields_no_kill() -> None:
    state = _state(_all_living_seats())
    submissions = {
        "P01": _kill("P03"),
        "P02": _kill("P04"),
    }
    result = resolve_mafia_kill(state, submissions)
    assert result.target is None
    assert result.vote_tally == {"P03": 1, "P04": 1}
    assert result.reason == REASON_TIE


def test_mafia_cannot_target_mafia() -> None:
    state = _state(_all_living_seats())
    submissions = {
        "P01": _kill("P02"),  # invalid — mafia target
        "P02": _kill("P03"),
    }
    result = resolve_mafia_kill(state, submissions)
    assert result.vote_tally == {"P03": 1}
    assert result.target == "P03"
    assert result.reason == REASON_UNIQUE_PLURALITY


def test_dead_mafia_submission_ignored() -> None:
    seats = (
        _seat("P01", 0, Role.MAFIA_GOON, Faction.MAFIA, alive=False),
        _seat("P02", 1, Role.MAFIA_GOON, Faction.MAFIA),
        _seat("P03", 2, Role.DETECTIVE, Faction.TOWN),
        _seat("P04", 3, Role.DOCTOR, Faction.TOWN),
        _seat("P05", 4, Role.VILLAGER, Faction.TOWN),
        _seat("P06", 5, Role.VILLAGER, Faction.TOWN),
        _seat("P07", 6, Role.VILLAGER, Faction.TOWN),
    )
    state = _state(seats)
    submissions = {
        "P01": _kill("P05"),  # dead voter — ignored
        "P02": _kill("P03"),
    }
    result = resolve_mafia_kill(state, submissions)
    assert result.vote_tally == {"P03": 1}
    assert result.target == "P03"


def test_missing_submissions_tolerated() -> None:
    state = _state(_all_living_seats())
    submissions = {
        "P01": _kill("P05"),
        # P02 missing
    }
    result = resolve_mafia_kill(state, submissions)
    assert result.vote_tally == {"P05": 1}
    assert result.target == "P05"
    assert result.reason == REASON_UNIQUE_PLURALITY


def test_target_dead_is_invalid() -> None:
    seats = (
        _seat("P01", 0, Role.MAFIA_GOON, Faction.MAFIA),
        _seat("P02", 1, Role.MAFIA_GOON, Faction.MAFIA),
        _seat("P03", 2, Role.DETECTIVE, Faction.TOWN, alive=False),
        _seat("P04", 3, Role.DOCTOR, Faction.TOWN),
        _seat("P05", 4, Role.VILLAGER, Faction.TOWN),
        _seat("P06", 5, Role.VILLAGER, Faction.TOWN),
        _seat("P07", 6, Role.VILLAGER, Faction.TOWN),
    )
    state = _state(seats)
    submissions = {
        "P01": _kill("P03"),  # dead — invalid
        "P02": _kill("P04"),
    }
    result = resolve_mafia_kill(state, submissions)
    assert result.vote_tally == {"P04": 1}
    assert result.target == "P04"


def test_self_target_invalid() -> None:
    state = _state(_all_living_seats())
    submissions = {
        "P01": _kill("P01"),  # self — also mafia, invalid
        "P02": _kill("P05"),
    }
    result = resolve_mafia_kill(state, submissions)
    assert result.vote_tally == {"P05": 1}
    assert result.target == "P05"


def test_nonexistent_target_invalid() -> None:
    state = _state(_all_living_seats())
    submissions = {
        "P01": _kill("P99"),
        "P02": _kill("P03"),
    }
    result = resolve_mafia_kill(state, submissions)
    assert result.vote_tally == {"P03": 1}
    assert result.target == "P03"


def test_non_kill_action_ignored() -> None:
    state = _state(_all_living_seats())
    submissions = {
        "P01": Action(type=ActionType.NOOP),
        "P02": Action(type=ActionType.VOTE, target="P03"),
    }
    result = resolve_mafia_kill(state, submissions)
    assert result.vote_tally == {}
    assert result.target is None
    assert result.reason == REASON_ALL_INVALID


def test_empty_submissions_no_kill() -> None:
    state = _state(_all_living_seats())
    result = resolve_mafia_kill(state, {})
    assert result.target is None
    assert result.vote_tally == {}
    assert result.reason == REASON_ALL_INVALID


def test_town_submissions_ignored() -> None:
    state = _state(_all_living_seats())
    submissions = {
        "P03": _kill("P01"),  # detective — not mafia, ignored
        "P04": _kill("P01"),
        "P01": _kill("P05"),
        "P02": _kill("P05"),
    }
    result = resolve_mafia_kill(state, submissions)
    assert result.vote_tally == {"P05": 2}
    assert result.target == "P05"


def test_chat_field_is_never_read() -> None:
    fields = set(Action.model_fields.keys())
    assert fields == {"type", "target"}


def test_result_is_immutable() -> None:
    state = _state(_all_living_seats())
    result = resolve_mafia_kill(state, {"P01": _kill("P05"), "P02": _kill("P05")})
    with pytest.raises(ValidationError):
        result.target = "P99"  # type: ignore[misc]
