"""Win-condition checker for the deterministic engine.

``check_win`` is a pure function over ``GameState`` and a ruleset. Rulesets may
declare an ordered ``WIN_CONDITIONS`` tuple to describe team, solo-faction, and
alternate win policies. If a legacy test stub omits that tuple, the checker uses
the original canonical two-faction policy:

1. TOWN wins when no mafia are alive.
2. MAFIA wins when alive mafia >= alive town (parity rule).
3. DRAW when the current day has exceeded ``MAX_DAYS`` with no prior winner.

Otherwise the game is undecided and ``None`` is returned. The function never
mutates state and is independent of ``state.terminal_result``, so calling it
again after the engine has stamped a terminal outcome yields the same answer.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Protocol

from pydantic import BaseModel, ConfigDict

from padrino.core.engine.state import GameState
from padrino.core.enums import Faction

REASON_ALL_MAFIA_ELIMINATED: Final[str] = "ALL_MAFIA_ELIMINATED"
REASON_PARITY_REACHED: Final[str] = "PARITY_REACHED"
REASON_MAX_DAYS_REACHED: Final[str] = "MAX_DAYS_REACHED"


class WinConditionKind(StrEnum):
    """Supported deterministic terminal-condition evaluators."""

    TARGET_FACTIONS_ELIMINATED = "TARGET_FACTIONS_ELIMINATED"
    FACTION_PARITY = "FACTION_PARITY"
    SOLO_LAST_ALIVE = "SOLO_LAST_ALIVE"
    ALT_TRIGGER = "ALT_TRIGGER"
    DAY_CAP = "DAY_CAP"


class WinCondition(BaseModel):
    """One ordered terminal-condition declaration for a ruleset."""

    model_config = ConfigDict(frozen=True)

    kind: WinConditionKind
    winner: str
    reason: str
    faction: Faction | None = None
    target_factions: tuple[Faction, ...] = ()
    opponent_factions: tuple[Faction, ...] = ()
    blocked_by_alive_factions: tuple[Faction, ...] = ()
    trigger: str | None = None


class WinResult(BaseModel):
    """Outcome of a win-condition check. Immutable."""

    model_config = ConfigDict(frozen=True)

    winner: str
    reason: str


class Ruleset(Protocol):
    """Structural ruleset interface required by the win checker."""

    MAX_DAYS: int


def canonical_two_faction_win_conditions() -> tuple[WinCondition, ...]:
    """Return the byte-stable Town/Mafia/DRAW policy used by canonical rulesets."""
    return (
        WinCondition(
            kind=WinConditionKind.TARGET_FACTIONS_ELIMINATED,
            winner=Faction.TOWN.value,
            reason=REASON_ALL_MAFIA_ELIMINATED,
            target_factions=(Faction.MAFIA,),
        ),
        WinCondition(
            kind=WinConditionKind.FACTION_PARITY,
            winner=Faction.MAFIA.value,
            reason=REASON_PARITY_REACHED,
            faction=Faction.MAFIA,
            opponent_factions=(Faction.TOWN,),
        ),
        WinCondition(
            kind=WinConditionKind.DAY_CAP,
            winner="DRAW",
            reason=REASON_MAX_DAYS_REACHED,
        ),
    )


def check_win(state: GameState, ruleset: Ruleset) -> WinResult | None:
    """Return the resolved `WinResult` or `None` if the game continues."""
    for condition in _conditions_for(ruleset):
        if _condition_matches(condition, state, ruleset):
            return WinResult(winner=condition.winner, reason=condition.reason)
    return None


def _conditions_for(ruleset: Ruleset) -> tuple[WinCondition, ...]:
    declared = getattr(ruleset, "WIN_CONDITIONS", None)
    if declared is None:
        return canonical_two_faction_win_conditions()
    return tuple(declared)


def _condition_matches(condition: WinCondition, state: GameState, ruleset: Ruleset) -> bool:
    if condition.kind is WinConditionKind.TARGET_FACTIONS_ELIMINATED:
        return _target_factions_eliminated(condition, state)
    if condition.kind is WinConditionKind.FACTION_PARITY:
        return _faction_parity_reached(condition, state)
    if condition.kind is WinConditionKind.SOLO_LAST_ALIVE:
        return _solo_last_alive(condition, state)
    if condition.kind is WinConditionKind.ALT_TRIGGER:
        return condition.trigger is not None and condition.trigger in state.win_condition_triggers
    if condition.kind is WinConditionKind.DAY_CAP:
        return state.day > ruleset.MAX_DAYS
    return False


def _alive_count(state: GameState, factions: tuple[Faction, ...]) -> int:
    return sum(state.alive_count_by_faction(faction) for faction in factions)


def _total_alive(state: GameState) -> int:
    return sum(state.alive_counts_by_faction().values())


def _target_factions_eliminated(condition: WinCondition, state: GameState) -> bool:
    return bool(condition.target_factions) and _alive_count(state, condition.target_factions) == 0


def _faction_parity_reached(condition: WinCondition, state: GameState) -> bool:
    if condition.faction is None or not condition.opponent_factions:
        return False
    if condition.blocked_by_alive_factions and _alive_count(
        state, condition.blocked_by_alive_factions
    ):
        return False
    alive_faction = state.alive_count_by_faction(condition.faction)
    if alive_faction <= 0:
        return False
    return alive_faction >= _alive_count(state, condition.opponent_factions)


def _solo_last_alive(condition: WinCondition, state: GameState) -> bool:
    if condition.faction is None:
        return False
    alive_faction = state.alive_count_by_faction(condition.faction)
    return alive_faction > 0 and alive_faction == _total_alive(state)


__all__ = [
    "REASON_ALL_MAFIA_ELIMINATED",
    "REASON_MAX_DAYS_REACHED",
    "REASON_PARITY_REACHED",
    "Ruleset",
    "WinCondition",
    "WinConditionKind",
    "WinResult",
    "canonical_two_faction_win_conditions",
    "check_win",
]
