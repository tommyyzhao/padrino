"""Tests for the day vote resolver."""

from __future__ import annotations

from padrino.core.engine.actions import Action
from padrino.core.engine.resolvers.day_vote import (
    REASON_ALL_ABSTAIN,
    REASON_TIE,
    REASON_UNIQUE_PLURALITY,
    DayVoteResult,
    resolve_day_vote,
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
        current_phase=Phase(kind=PhaseKind.DAY_VOTE, day=1, round=0),
        seats=seats,
        day=1,
    )


def _vote(target: str) -> Action:
    return Action(type=ActionType.VOTE, target=target)


def test_unique_winner_is_eliminated() -> None:
    state = _state(_all_living_seats())
    submissions = {
        "P01": _vote("P03"),
        "P02": _vote("P03"),
        "P03": _vote("P01"),
        "P04": _vote("P03"),
        "P05": Action(type=ActionType.ABSTAIN),
        "P06": _vote("P03"),
        "P07": _vote("P05"),
    }
    result = resolve_day_vote(state, submissions)
    assert isinstance(result, DayVoteResult)
    assert result.eliminated == "P03"
    assert result.vote_tally == {"P03": 4, "P01": 1, "P05": 1}
    assert result.reason == REASON_UNIQUE_PLURALITY


def test_tie_yields_no_elimination() -> None:
    state = _state(_all_living_seats())
    submissions = {
        "P01": _vote("P03"),
        "P02": _vote("P03"),
        "P03": _vote("P01"),
        "P04": _vote("P01"),
        "P05": Action(type=ActionType.ABSTAIN),
        "P06": Action(type=ActionType.ABSTAIN),
        "P07": Action(type=ActionType.ABSTAIN),
    }
    result = resolve_day_vote(state, submissions)
    assert result.eliminated is None
    assert result.vote_tally == {"P03": 2, "P01": 2}
    assert result.reason == REASON_TIE


def test_all_abstain_yields_no_elimination() -> None:
    state = _state(_all_living_seats())
    submissions = {pid: Action(type=ActionType.ABSTAIN) for pid in ("P01", "P02", "P03")}
    result = resolve_day_vote(state, submissions)
    assert result.eliminated is None
    assert result.vote_tally == {}
    assert result.reason == REASON_ALL_ABSTAIN


def test_self_vote_becomes_abstain() -> None:
    state = _state(_all_living_seats())
    submissions = {
        "P01": _vote("P01"),
        "P02": _vote("P03"),
    }
    result = resolve_day_vote(state, submissions)
    assert result.vote_tally == {"P03": 1}
    assert result.eliminated == "P03"


def test_vote_for_dead_becomes_abstain() -> None:
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
        "P01": _vote("P03"),  # dead → abstain
        "P02": _vote("P03"),  # dead → abstain
        "P04": _vote("P01"),
    }
    result = resolve_day_vote(state, submissions)
    assert result.vote_tally == {"P01": 1}
    assert result.eliminated == "P01"


def test_missing_submission_is_abstain() -> None:
    state = _state(_all_living_seats())
    submissions: dict[str, Action] = {
        "P01": _vote("P03"),
        # P02..P07 missing
    }
    result = resolve_day_vote(state, submissions)
    assert result.vote_tally == {"P03": 1}
    assert result.eliminated == "P03"
    assert result.reason == REASON_UNIQUE_PLURALITY


def test_vote_for_nonexistent_target_becomes_abstain() -> None:
    state = _state(_all_living_seats())
    submissions = {
        "P01": _vote("P99"),  # not a real seat
        "P02": _vote("P03"),
    }
    result = resolve_day_vote(state, submissions)
    assert result.vote_tally == {"P03": 1}
    assert result.eliminated == "P03"


def test_submissions_from_dead_players_are_ignored() -> None:
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
        "P01": _vote("P03"),  # dead voter ignored
        "P02": _vote("P04"),
    }
    result = resolve_day_vote(state, submissions)
    assert result.vote_tally == {"P04": 1}
    assert result.eliminated == "P04"


def test_non_vote_action_type_is_abstain() -> None:
    state = _state(_all_living_seats())
    submissions = {
        "P01": Action(type=ActionType.NOOP),
        "P02": Action(type=ActionType.MAFIA_KILL, target="P03"),
        "P03": _vote("P01"),
    }
    result = resolve_day_vote(state, submissions)
    assert result.vote_tally == {"P01": 1}
    assert result.eliminated == "P01"


def test_chat_field_is_never_read() -> None:
    """The Action model carries only mechanical fields — no chat / message / memory."""
    fields = set(Action.model_fields.keys())
    assert fields == {"type", "target"}


def test_result_is_immutable() -> None:
    state = _state(_all_living_seats())
    result = resolve_day_vote(state, {"P01": _vote("P03")})
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        result.eliminated = "P99"  # type: ignore[misc]


def test_empty_submissions_is_all_abstain() -> None:
    state = _state(_all_living_seats())
    result = resolve_day_vote(state, {})
    assert result.eliminated is None
    assert result.vote_tally == {}
    assert result.reason == REASON_ALL_ABSTAIN
