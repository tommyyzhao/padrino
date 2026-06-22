"""Tests for the win-condition checker."""

from __future__ import annotations

from typing import Protocol

import pytest
from pydantic import ValidationError

from padrino.core.engine.event_log import EventLog
from padrino.core.engine.state import GameState, Phase, Seat
from padrino.core.engine.win_conditions import (
    REASON_ALL_MAFIA_ELIMINATED,
    REASON_MAX_DAYS_REACHED,
    REASON_PARITY_REACHED,
    WinCondition,
    WinConditionKind,
    WinResult,
    check_win,
)
from padrino.core.enums import Faction, PhaseKind, Role
from padrino.core.rulesets import bench10_v1, mini7_v1, sk12_v1


class _HasMaxDays(Protocol):
    MAX_DAYS: int


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
    win_condition_triggers: tuple[str, ...] = (),
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
        win_condition_triggers=win_condition_triggers,
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


def _full_bench10_seats(
    *,
    mafia_alive: int = 3,
    town_alive: int = 7,
) -> tuple[Seat, ...]:
    """Build a 10-seat bench10 lineup with the requested alive counts."""
    seats: list[Seat] = []
    mafia_template = [(Role.MAFIA_GOON, Faction.MAFIA)] * 3
    town_template = [
        (Role.DETECTIVE, Faction.TOWN),
        (Role.DOCTOR, Faction.TOWN),
        (Role.VILLAGER, Faction.TOWN),
        (Role.VILLAGER, Faction.TOWN),
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


def _sk12_seats(
    *,
    mafia_alive: int = 3,
    town_alive: int = 8,
    sk_alive: int = 1,
) -> tuple[Seat, ...]:
    """Build a 12-seat SK setup with the requested alive counts."""
    seats: list[Seat] = []
    template = [
        *[(Role.MAFIA_GOON, Faction.MAFIA)] * 3,
        (Role.SERIAL_KILLER, Faction.SERIAL_KILLER),
        (Role.DETECTIVE, Faction.TOWN),
        (Role.DOCTOR, Faction.TOWN),
        *[(Role.VILLAGER, Faction.TOWN)] * 6,
    ]
    remaining = {
        Faction.MAFIA: mafia_alive,
        Faction.TOWN: town_alive,
        Faction.SERIAL_KILLER: sk_alive,
    }
    for idx, (role, faction) in enumerate(template):
        alive = remaining[faction] > 0
        if alive:
            remaining[faction] -= 1
        seats.append(_seat(f"P{idx + 1:02d}", idx, role, faction, alive=alive))
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


def test_sk_ruleset_does_not_reuse_two_faction_mafia_parity_while_sk_alive() -> None:
    seats = _sk12_seats(mafia_alive=2, town_alive=2, sk_alive=1)
    result = check_win(_state(seats, day=3), sk12_v1)

    assert result is None


def test_sk_ruleset_serial_killer_wins_when_last_alive() -> None:
    seats = _sk12_seats(mafia_alive=0, town_alive=0, sk_alive=1)
    result = check_win(_state(seats, day=4), sk12_v1)

    assert result == WinResult(winner="SERIAL_KILLER", reason="SOLO_LAST_ALIVE")


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


def test_three_faction_policy_blocks_two_faction_parity_until_solo_dead() -> None:
    class SkRuleset:
        MAX_DAYS = 5
        WIN_CONDITIONS = (
            WinCondition(
                kind=WinConditionKind.TARGET_FACTIONS_ELIMINATED,
                winner="TOWN",
                reason="ALL_HOSTILES_ELIMINATED",
                target_factions=(Faction.MAFIA, Faction.SERIAL_KILLER),
            ),
            WinCondition(
                kind=WinConditionKind.SOLO_LAST_ALIVE,
                winner="SERIAL_KILLER",
                reason="SOLO_LAST_ALIVE",
                faction=Faction.SERIAL_KILLER,
            ),
            WinCondition(
                kind=WinConditionKind.FACTION_PARITY,
                winner="MAFIA",
                reason=REASON_PARITY_REACHED,
                faction=Faction.MAFIA,
                opponent_factions=(Faction.TOWN,),
                blocked_by_alive_factions=(Faction.SERIAL_KILLER,),
            ),
            WinCondition(
                kind=WinConditionKind.DAY_CAP,
                winner="DRAW",
                reason=REASON_MAX_DAYS_REACHED,
            ),
        )

    state = _state(
        (
            _seat("P01", 0, Role.MAFIA_GOON, Faction.MAFIA),
            _seat("P02", 1, Role.VILLAGER, Faction.TOWN),
            _seat("P03", 2, Role.VILLAGER, Faction.SERIAL_KILLER),
        )
    )

    assert check_win(state, SkRuleset()) is None


def test_solo_faction_wins_when_last_alive() -> None:
    class SkRuleset:
        MAX_DAYS = 5
        WIN_CONDITIONS = (
            WinCondition(
                kind=WinConditionKind.SOLO_LAST_ALIVE,
                winner="SERIAL_KILLER",
                reason="SOLO_LAST_ALIVE",
                faction=Faction.SERIAL_KILLER,
            ),
        )

    state = _state(
        (
            _seat("P01", 0, Role.MAFIA_GOON, Faction.MAFIA, alive=False),
            _seat("P02", 1, Role.VILLAGER, Faction.TOWN, alive=False),
            _seat("P03", 2, Role.VILLAGER, Faction.SERIAL_KILLER),
        )
    )

    result = check_win(state, SkRuleset())

    assert result == WinResult(winner="SERIAL_KILLER", reason="SOLO_LAST_ALIVE")


def test_alt_win_policy_resolves_from_state_trigger() -> None:
    class JesterRuleset:
        MAX_DAYS = 5
        WIN_CONDITIONS = (
            WinCondition(
                kind=WinConditionKind.ALT_TRIGGER,
                winner="JESTER",
                reason="JESTER_LYNCHED",
                trigger="JESTER_LYNCHED",
            ),
        )

    state = _state(
        _full_mini7_seats(mafia_alive=2, town_alive=5),
        win_condition_triggers=("JESTER_LYNCHED",),
    )

    result = check_win(state, JesterRuleset())

    assert result == WinResult(winner="JESTER", reason="JESTER_LYNCHED")


def _legacy_check_win(state: GameState, ruleset: _HasMaxDays) -> WinResult | None:
    alive_mafia = state.alive_count_by_faction(Faction.MAFIA)
    alive_town = state.alive_count_by_faction(Faction.TOWN)

    if alive_mafia == 0:
        return WinResult(winner="TOWN", reason=REASON_ALL_MAFIA_ELIMINATED)
    if alive_mafia >= alive_town:
        return WinResult(winner="MAFIA", reason=REASON_PARITY_REACHED)
    if state.day > ruleset.MAX_DAYS:
        return WinResult(winner="DRAW", reason=REASON_MAX_DAYS_REACHED)
    return None


def _terminal_hash_chain_for(win: WinResult | None) -> tuple[str, ...]:
    log = EventLog()
    log.append(
        {
            "event_type": "GameCreated",
            "sequence": 0,
            "phase": "SETUP",
            "visibility": "SYSTEM",
            "actor_player_id": None,
            "payload": {
                "ruleset_id": "byte-stability",
                "game_id": "G-BYTE-STABILITY",
                "game_seed": "seed-byte-stability",
                "player_count": 7,
            },
        }
    )
    if win is not None:
        log.append(
            {
                "event_type": "GameTerminated",
                "sequence": 1,
                "phase": "TERMINAL",
                "visibility": "PUBLIC",
                "actor_player_id": None,
                "payload": {"winner": win.winner, "reason": win.reason},
            }
        )
    return tuple(stored.event_hash for stored in log.events)


@pytest.mark.parametrize(
    ("ruleset", "seats", "day"),
    [
        (mini7_v1, _full_mini7_seats(mafia_alive=0, town_alive=4), 2),
        (mini7_v1, _full_mini7_seats(mafia_alive=1, town_alive=1), 3),
        (mini7_v1, _full_mini7_seats(mafia_alive=1, town_alive=2), mini7_v1.MAX_DAYS + 1),
        (mini7_v1, _full_mini7_seats(mafia_alive=2, town_alive=5), 1),
        (mini7_v1, _full_mini7_seats(mafia_alive=0, town_alive=0), 3),
        (
            mini7_v1,
            _full_mini7_seats(mafia_alive=1, town_alive=1),
            mini7_v1.MAX_DAYS + 2,
        ),
        (mini7_v1, _full_mini7_seats(mafia_alive=0, town_alive=2), mini7_v1.MAX_DAYS + 2),
        (mini7_v1, _full_mini7_seats(mafia_alive=2, town_alive=3), 2),
        (
            mini7_v1,
            _full_mini7_seats(mafia_alive=2, town_alive=3),
            mini7_v1.MAX_DAYS + 1,
        ),
        (mini7_v1, _full_mini7_seats(mafia_alive=2, town_alive=1), 3),
        (bench10_v1, _full_bench10_seats(mafia_alive=0, town_alive=6), 2),
        (bench10_v1, _full_bench10_seats(mafia_alive=2, town_alive=2), 4),
        (
            bench10_v1,
            _full_bench10_seats(mafia_alive=1, town_alive=2),
            bench10_v1.MAX_DAYS + 1,
        ),
        (bench10_v1, _full_bench10_seats(mafia_alive=3, town_alive=7), 1),
        (bench10_v1, _full_bench10_seats(mafia_alive=3, town_alive=4), 4),
    ],
)
def test_builtin_two_faction_policy_matches_legacy_results_and_hashes(
    ruleset: _HasMaxDays,
    seats: tuple[Seat, ...],
    day: int,
) -> None:
    state = _state(seats, day=day)

    legacy = _legacy_check_win(state, ruleset)
    generalized = check_win(state, ruleset)

    assert generalized == legacy
    assert _terminal_hash_chain_for(generalized) == _terminal_hash_chain_for(legacy)
