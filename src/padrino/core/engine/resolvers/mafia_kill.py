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
from padrino.core.engine.resolvers import nar as _nar
from padrino.core.engine.state import GameState

REASON_UNIQUE_PLURALITY = _nar.REASON_UNIQUE_PLURALITY
REASON_TIE = _nar.REASON_TIE
REASON_ALL_INVALID = _nar.REASON_ALL_INVALID


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
    result = _nar.resolve_current_mafia_kill(state, mafia_submissions)
    return MafiaKillResult(
        target=result.target,
        vote_tally=dict(result.vote_tally),
        reason=result.reason,
    )
