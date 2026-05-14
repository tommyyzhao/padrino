"""Detective night investigate resolver.

Pure function: given a `GameState` and at most one `Action` submitted by the
detective, return a `DetectiveResult` describing whether the investigation
resolved and what alignment the target carries.

Rules enforced here:
- A dead detective or missing submission yields no finding.
- The submitted action must be `INVESTIGATE` against a living seat that is
  not the detective itself.
- Finding is `'MAFIA'` if the target's faction is MAFIA, else `'TOWN'`.

Queuing the result for next-day delivery (and suppressing it if the detective
dies the same night) belongs to the night-resolution composer (US-013), not
this resolver.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from padrino.core.engine.actions import Action
from padrino.core.engine.state import GameState, Seat
from padrino.core.enums import ActionType, Faction, Role

FINDING_MAFIA = "MAFIA"
FINDING_TOWN = "TOWN"

REASON_RESOLVED = "resolved"
REASON_SELF_TARGET = "self_target"
REASON_INVALID_TARGET = "invalid_target"
REASON_NO_SUBMISSION = "no_submission"
REASON_DEAD_DETECTIVE = "dead_detective"
REASON_NO_DETECTIVE = "no_detective"


class DetectiveResult(BaseModel):
    """Outcome of resolving the detective's night investigation. Immutable."""

    model_config = ConfigDict(frozen=True)

    target: str | None
    finding: str | None
    reason: str


def _find_detective(state: GameState) -> Seat | None:
    for seat in state.seats:
        if seat.role is Role.DETECTIVE:
            return seat
    return None


def resolve_detective_investigation(
    state: GameState,
    detective_submission: Action | None,
) -> DetectiveResult:
    """Resolve the detective's night investigation and return the outcome."""
    detective = _find_detective(state)
    if detective is None:
        return DetectiveResult(target=None, finding=None, reason=REASON_NO_DETECTIVE)
    if not detective.alive:
        return DetectiveResult(target=None, finding=None, reason=REASON_DEAD_DETECTIVE)
    if detective_submission is None:
        return DetectiveResult(target=None, finding=None, reason=REASON_NO_SUBMISSION)
    if detective_submission.type is not ActionType.INVESTIGATE:
        return DetectiveResult(target=None, finding=None, reason=REASON_INVALID_TARGET)

    target_id = detective_submission.target
    if target_id is None:
        return DetectiveResult(target=None, finding=None, reason=REASON_INVALID_TARGET)

    if target_id == detective.public_player_id:
        return DetectiveResult(target=None, finding=None, reason=REASON_SELF_TARGET)

    target_seat = state.seat_by_public_id(target_id)
    if target_seat is None or not target_seat.alive:
        return DetectiveResult(target=None, finding=None, reason=REASON_INVALID_TARGET)

    finding = FINDING_MAFIA if target_seat.faction is Faction.MAFIA else FINDING_TOWN
    return DetectiveResult(target=target_id, finding=finding, reason=REASON_RESOLVED)
