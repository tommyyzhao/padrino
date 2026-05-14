"""Day vote resolver.

Pure function: given a `GameState` and a mapping of `public_player_id` to
`Action`, return a `DayVoteResult` describing the elimination outcome.

Invalid voter submissions (dead voter, missing, non-VOTE action type, target
that is absent / dead / self) are silently converted to ABSTAIN. Per the PRD
mini7_v1 ruleset, the unique-plurality voter is eliminated; ties or all
abstentions yield no elimination.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from padrino.core.engine.actions import Action
from padrino.core.engine.state import GameState
from padrino.core.enums import ActionType

REASON_UNIQUE_PLURALITY = "unique_plurality"
REASON_TIE = "tie"
REASON_ALL_ABSTAIN = "all_abstain"


class DayVoteResult(BaseModel):
    """Outcome of resolving a day vote. Immutable."""

    model_config = ConfigDict(frozen=True)

    eliminated: str | None
    vote_tally: dict[str, int]
    reason: str


def resolve_day_vote(
    state: GameState,
    submissions: Mapping[str, Action],
) -> DayVoteResult:
    """Resolve the day vote and return the elimination result."""
    living_ids = {s.public_player_id for s in state.seats if s.alive}

    tally: dict[str, int] = {}
    for seat in state.seats:
        if not seat.alive:
            continue
        action = submissions.get(seat.public_player_id)
        if action is None or action.type is not ActionType.VOTE:
            continue
        target = action.target
        if target is None or target == seat.public_player_id or target not in living_ids:
            continue
        tally[target] = tally.get(target, 0) + 1

    if not tally:
        return DayVoteResult(eliminated=None, vote_tally={}, reason=REASON_ALL_ABSTAIN)

    top_count = max(tally.values())
    winners = [pid for pid, count in tally.items() if count == top_count]
    if len(winners) == 1:
        return DayVoteResult(
            eliminated=winners[0],
            vote_tally=tally,
            reason=REASON_UNIQUE_PLURALITY,
        )
    return DayVoteResult(eliminated=None, vote_tally=tally, reason=REASON_TIE)
