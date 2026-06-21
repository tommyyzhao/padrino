"""Tests for the night resolution composer."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from padrino.core.engine.actions import Action
from padrino.core.engine.resolvers.night import NightResolution, resolve_night
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
        game_seed="seed-night",
        current_phase=Phase(kind=PhaseKind.NIGHT_ACTIONS, day=1, round=0),
        seats=seats,
        day=1,
    )


def _kill(target: str) -> Action:
    return Action(type=ActionType.MAFIA_KILL, target=target)


def _protect(target: str) -> Action:
    return Action(type=ActionType.PROTECT, target=target)


def _investigate(target: str) -> Action:
    return Action(type=ActionType.INVESTIGATE, target=target)


def _roleblock(target: str) -> Action:
    return Action(type=ActionType.ROLEBLOCK, target=target)


def test_protect_saves_target() -> None:
    state = _state(_all_living_seats())
    submissions = {
        "P01": _kill("P05"),
        "P02": _kill("P05"),
        "P04": _protect("P05"),
        "P03": _investigate("P01"),
    }
    result = resolve_night(state, submissions)
    assert isinstance(result, NightResolution)
    assert result.mafia_kill_target == "P05"
    assert result.protected == "P05"
    assert result.eliminated is None
    assert result.detective_finding == ("P01", "MAFIA")


def test_protect_mismatched_death_occurs() -> None:
    state = _state(_all_living_seats())
    submissions = {
        "P01": _kill("P05"),
        "P02": _kill("P05"),
        "P04": _protect("P06"),
        "P03": _investigate("P07"),
    }
    result = resolve_night(state, submissions)
    assert result.mafia_kill_target == "P05"
    assert result.protected == "P06"
    assert result.eliminated == "P05"
    assert result.detective_finding == ("P07", "TOWN")


def test_no_kill_no_protect() -> None:
    state = _state(_all_living_seats())
    submissions: dict[str, Action] = {}
    result = resolve_night(state, submissions)
    assert result.mafia_kill_target is None
    assert result.protected is None
    assert result.eliminated is None
    assert result.detective_finding is None


def test_detective_dies_same_night_finding_suppressed() -> None:
    state = _state(_all_living_seats())
    submissions = {
        "P01": _kill("P03"),  # kill the detective
        "P02": _kill("P03"),
        "P04": _protect("P05"),  # doctor protects someone else
        "P03": _investigate("P01"),
    }
    result = resolve_night(state, submissions)
    assert result.mafia_kill_target == "P03"
    assert result.protected == "P05"
    assert result.eliminated == "P03"
    assert result.detective_finding is None


def test_detective_lives_finding_queued() -> None:
    state = _state(_all_living_seats())
    submissions = {
        "P01": _kill("P03"),
        "P02": _kill("P03"),
        "P04": _protect("P03"),  # doctor saves detective
        "P03": _investigate("P02"),
    }
    result = resolve_night(state, submissions)
    assert result.mafia_kill_target == "P03"
    assert result.protected == "P03"
    assert result.eliminated is None
    assert result.detective_finding == ("P02", "MAFIA")


def test_detective_investigates_town() -> None:
    state = _state(_all_living_seats())
    submissions = {
        "P03": _investigate("P05"),
    }
    result = resolve_night(state, submissions)
    assert result.eliminated is None
    assert result.detective_finding == ("P05", "TOWN")


def test_mafia_tie_no_kill_protect_irrelevant() -> None:
    state = _state(_all_living_seats())
    submissions = {
        "P01": _kill("P05"),
        "P02": _kill("P06"),
        "P04": _protect("P05"),
    }
    result = resolve_night(state, submissions)
    assert result.mafia_kill_target is None
    assert result.protected == "P05"
    assert result.eliminated is None


def test_dead_detective_no_finding() -> None:
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
        "P01": _kill("P05"),
        "P02": _kill("P05"),
        "P03": _investigate("P01"),  # dead, ignored by detective resolver
    }
    result = resolve_night(state, submissions)
    assert result.eliminated == "P05"
    assert result.detective_finding is None


def test_doctor_repeat_violation_does_not_save() -> None:
    seats = (
        _seat("P01", 0, Role.MAFIA_GOON, Faction.MAFIA),
        _seat("P02", 1, Role.MAFIA_GOON, Faction.MAFIA),
        _seat("P03", 2, Role.DETECTIVE, Faction.TOWN),
        _seat("P04", 3, Role.DOCTOR, Faction.TOWN, last_protected_target="P05"),
        _seat("P05", 4, Role.VILLAGER, Faction.TOWN),
        _seat("P06", 5, Role.VILLAGER, Faction.TOWN),
        _seat("P07", 6, Role.VILLAGER, Faction.TOWN),
    )
    state = _state(seats)
    submissions = {
        "P01": _kill("P05"),
        "P02": _kill("P05"),
        "P04": _protect("P05"),  # repeat — refused
    }
    result = resolve_night(state, submissions)
    assert result.mafia_kill_target == "P05"
    assert result.protected is None
    assert result.eliminated == "P05"


def test_town_submissions_to_kill_ignored() -> None:
    state = _state(_all_living_seats())
    submissions = {
        "P03": _kill("P01"),  # detective trying to kill — ignored
        "P05": _kill("P01"),
    }
    result = resolve_night(state, submissions)
    assert result.mafia_kill_target is None
    assert result.eliminated is None


def test_non_roleblocker_submission_to_roleblock_is_ignored() -> None:
    seats = (
        _seat("P01", 0, Role.MAFIA_GOON, Faction.MAFIA),
        _seat("P02", 1, Role.MAFIA_GOON, Faction.MAFIA),
        _seat("P03", 2, Role.MAFIA_ROLEBLOCKER, Faction.MAFIA),
        _seat("P04", 3, Role.DETECTIVE, Faction.TOWN),
        _seat("P05", 4, Role.DOCTOR, Faction.TOWN),
        _seat("P06", 5, Role.VILLAGER, Faction.TOWN),
        _seat("P07", 6, Role.VILLAGER, Faction.TOWN),
        _seat("P08", 7, Role.VILLAGER, Faction.TOWN),
        _seat("P09", 8, Role.VILLAGER, Faction.TOWN),
        _seat("P10", 9, Role.VILLAGER, Faction.TOWN),
    )
    state = _state(seats)
    submissions = {
        "P01": _kill("P06"),
        "P02": _kill("P06"),
        "P04": _roleblock("P05"),  # detective is not a roleblocker; ignored
        "P05": _protect("P06"),
    }

    result = resolve_night(state, submissions)

    assert result.blocked_actor_ids == ()
    assert result.eliminated is None
    assert result.protected == "P06"


def test_mafia_roleblocker_blocks_active_doctor_action() -> None:
    seats = (
        _seat("P01", 0, Role.MAFIA_GOON, Faction.MAFIA),
        _seat("P02", 1, Role.MAFIA_GOON, Faction.MAFIA),
        _seat("P03", 2, Role.MAFIA_ROLEBLOCKER, Faction.MAFIA),
        _seat("P04", 3, Role.DETECTIVE, Faction.TOWN),
        _seat("P05", 4, Role.DOCTOR, Faction.TOWN),
        _seat("P06", 5, Role.VILLAGER, Faction.TOWN),
        _seat("P07", 6, Role.VILLAGER, Faction.TOWN),
        _seat("P08", 7, Role.VILLAGER, Faction.TOWN),
        _seat("P09", 8, Role.VILLAGER, Faction.TOWN),
        _seat("P10", 9, Role.VILLAGER, Faction.TOWN),
    )
    state = _state(seats)
    submissions = {
        "P01": _kill("P06"),
        "P02": _kill("P06"),
        "P03": _roleblock("P05"),
        "P05": _protect("P06"),
    }

    result = resolve_night(state, submissions)

    assert result.blocked_actor_ids == ("P05",)
    assert result.protected is None
    assert result.eliminated == "P06"
    feedback = result.feedback_by_code("ACTION_BLOCKED")[0]
    assert feedback.model_dump() == {
        "recipient": "P05",
        "code": "ACTION_BLOCKED",
        "message": "Your night action was blocked.",
        "target": "P06",
        "finding": None,
        "visited_player_ids": (),
        "visitor_player_ids": (),
    }


def test_chat_field_is_never_read() -> None:
    fields = set(Action.model_fields.keys())
    assert fields == {"type", "target"}


def test_result_is_immutable() -> None:
    state = _state(_all_living_seats())
    result = resolve_night(state, {})
    with pytest.raises(ValidationError):
        result.eliminated = "P05"  # type: ignore[misc]
