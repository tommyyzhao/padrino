"""Win-condition checker for the deterministic engine.

`check_win` is a pure function over `GameState` and a `Ruleset` Protocol. It
evaluates the three v1 outcomes in priority order:

1. TOWN wins when no mafia are alive.
2. MAFIA wins when alive mafia >= alive town (parity rule).
3. DRAW when the current day has exceeded `MAX_DAYS` with no prior winner.

Otherwise the game is undecided and `None` is returned. The function never
mutates state and is independent of `state.terminal_result`, so calling it
again after the engine has stamped a terminal outcome yields the same answer.
"""

from __future__ import annotations

from typing import Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from padrino.core.engine.state import GameState
from padrino.core.enums import Faction

REASON_ALL_MAFIA_ELIMINATED: Final[str] = "ALL_MAFIA_ELIMINATED"
REASON_PARITY_REACHED: Final[str] = "PARITY_REACHED"
REASON_MAX_DAYS_REACHED: Final[str] = "MAX_DAYS_REACHED"


class WinResult(BaseModel):
    """Outcome of a win-condition check. Immutable."""

    model_config = ConfigDict(frozen=True)

    winner: Literal["TOWN", "MAFIA", "DRAW"]
    reason: str


class Ruleset(Protocol):
    """Structural ruleset interface required by the win checker."""

    MAX_DAYS: int


def check_win(state: GameState, ruleset: Ruleset) -> WinResult | None:
    """Return the resolved `WinResult` or `None` if the game continues."""
    alive_mafia = state.alive_count_by_faction(Faction.MAFIA)
    alive_town = state.alive_count_by_faction(Faction.TOWN)

    if alive_mafia == 0:
        return WinResult(winner="TOWN", reason=REASON_ALL_MAFIA_ELIMINATED)
    if alive_mafia >= alive_town:
        return WinResult(winner="MAFIA", reason=REASON_PARITY_REACHED)
    if state.day > ruleset.MAX_DAYS:
        return WinResult(winner="DRAW", reason=REASON_MAX_DAYS_REACHED)
    return None
