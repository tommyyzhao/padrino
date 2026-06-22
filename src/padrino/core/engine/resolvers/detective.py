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
from padrino.core.engine.resolvers import nar as _nar
from padrino.core.engine.state import GameState

FINDING_MAFIA = _nar.FINDING_MAFIA
FINDING_TOWN = _nar.FINDING_TOWN

REASON_RESOLVED = _nar.REASON_RESOLVED
REASON_SELF_TARGET = _nar.REASON_SELF_TARGET
REASON_INVALID_TARGET = _nar.REASON_INVALID_TARGET
REASON_NO_SUBMISSION = _nar.REASON_NO_SUBMISSION
REASON_DEAD_DETECTIVE = _nar.REASON_DEAD_DETECTIVE
REASON_NO_DETECTIVE = _nar.REASON_NO_DETECTIVE


class DetectiveResult(BaseModel):
    """Outcome of resolving the detective's night investigation. Immutable."""

    model_config = ConfigDict(frozen=True)

    target: str | None
    finding: str | None
    reason: str


def resolve_detective_investigation(
    state: GameState,
    detective_submission: Action | None,
) -> DetectiveResult:
    """Resolve the detective's night investigation and return the outcome."""
    result = _nar.resolve_current_detective_investigation(state, detective_submission)
    return DetectiveResult(target=result.target, finding=result.finding, reason=result.reason)
