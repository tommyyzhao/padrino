"""Mafia night kill resolver.

Pure function: given a `GameState` and a mapping of `public_player_id` to
`Action`, return a `MafiaKillResult` describing which living non-mafia seat
the mafia targeted via unique-plurality vote among living mafia.

Submissions from dead seats, non-mafia seats, or seats whose action is not
`MAFIA_KILL` are silently discarded. Targets that are absent, dead, or
mafia-faction are likewise discarded. Unique plurality wins; ties or no
valid votes yield no kill.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from padrino.core.engine.actions import Action
from padrino.core.engine.state import GameState
from padrino.core.enums import ActionType, Faction

REASON_UNIQUE_PLURALITY = "unique_plurality"
REASON_TIE = "tie"
REASON_ALL_INVALID = "all_invalid"


class MafiaKillResult(BaseModel):
    """Outcome of resolving the mafia night kill. Immutable."""

    model_config = ConfigDict(frozen=True)

    target: str | None
    vote_tally: dict[str, int]
    reason: str


def resolve_mafia_kill(
    state: GameState,
    mafia_submissions: Mapping[str, Action],
) -> MafiaKillResult:
    """Resolve the mafia night kill and return the targeting result."""
    living_non_mafia: set[str] = {
        s.public_player_id for s in state.seats if s.alive and s.faction is not Faction.MAFIA
    }

    tally: dict[str, int] = {}
    for seat in state.seats:
        if not seat.alive or seat.faction is not Faction.MAFIA:
            continue
        action = mafia_submissions.get(seat.public_player_id)
        if action is None or action.type is not ActionType.MAFIA_KILL:
            continue
        target = action.target
        if target is None or target not in living_non_mafia:
            continue
        tally[target] = tally.get(target, 0) + 1

    if not tally:
        return MafiaKillResult(target=None, vote_tally={}, reason=REASON_ALL_INVALID)

    top_count = max(tally.values())
    winners = [pid for pid, count in tally.items() if count == top_count]
    if len(winners) == 1:
        return MafiaKillResult(
            target=winners[0],
            vote_tally=tally,
            reason=REASON_UNIQUE_PLURALITY,
        )
    return MafiaKillResult(target=None, vote_tally=tally, reason=REASON_TIE)
