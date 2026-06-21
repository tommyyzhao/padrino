"""Doctor night protect resolver.

Pure function: given a `GameState` and at most one `Action` submitted by the
doctor, return a `DoctorProtectResult` describing which seat (if any) the
doctor successfully protects this night.

Rules enforced here:
- A dead doctor or missing submission yields no protect.
- The submitted action must be `PROTECT` against a living seat.
- The doctor may not protect the same seat two nights in a row — the resolver
  reads the doctor seat's `last_protected_target` and refuses a repeat.
- Self-protect is permitted unless it was the previous night's target.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from padrino.core.engine.actions import Action
from padrino.core.engine.resolvers import nar as _nar
from padrino.core.engine.state import GameState

REASON_PROTECTED = _nar.REASON_PROTECTED
REASON_REPEAT_VIOLATION = _nar.REASON_REPEAT_VIOLATION
REASON_INVALID_TARGET = _nar.REASON_INVALID_TARGET
REASON_NO_SUBMISSION = _nar.REASON_NO_SUBMISSION
REASON_DEAD_DOCTOR = _nar.REASON_DEAD_DOCTOR
REASON_NO_DOCTOR = _nar.REASON_NO_DOCTOR


class DoctorProtectResult(BaseModel):
    """Outcome of resolving the doctor's night protect. Immutable."""

    model_config = ConfigDict(frozen=True)

    protected: str | None
    reason: str


def resolve_doctor_protect(
    state: GameState,
    doctor_submission: Action | None,
) -> DoctorProtectResult:
    """Resolve the doctor's night protect and return the outcome."""
    result = _nar.resolve_current_doctor_protect(state, doctor_submission)
    return DoctorProtectResult(protected=result.protected, reason=result.reason)
