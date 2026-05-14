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
from padrino.core.engine.state import GameState, Seat
from padrino.core.enums import ActionType, Role

REASON_PROTECTED = "protected"
REASON_REPEAT_VIOLATION = "REPEAT_VIOLATION"
REASON_INVALID_TARGET = "invalid_target"
REASON_NO_SUBMISSION = "no_submission"
REASON_DEAD_DOCTOR = "dead_doctor"
REASON_NO_DOCTOR = "no_doctor"


class DoctorProtectResult(BaseModel):
    """Outcome of resolving the doctor's night protect. Immutable."""

    model_config = ConfigDict(frozen=True)

    protected: str | None
    reason: str


def _find_doctor(state: GameState) -> Seat | None:
    for seat in state.seats:
        if seat.role is Role.DOCTOR:
            return seat
    return None


def resolve_doctor_protect(
    state: GameState,
    doctor_submission: Action | None,
) -> DoctorProtectResult:
    """Resolve the doctor's night protect and return the outcome."""
    doctor = _find_doctor(state)
    if doctor is None:
        return DoctorProtectResult(protected=None, reason=REASON_NO_DOCTOR)
    if not doctor.alive:
        return DoctorProtectResult(protected=None, reason=REASON_DEAD_DOCTOR)
    if doctor_submission is None:
        return DoctorProtectResult(protected=None, reason=REASON_NO_SUBMISSION)
    if doctor_submission.type is not ActionType.PROTECT:
        return DoctorProtectResult(protected=None, reason=REASON_INVALID_TARGET)

    target_id = doctor_submission.target
    if target_id is None:
        return DoctorProtectResult(protected=None, reason=REASON_INVALID_TARGET)

    target_seat = state.seat_by_public_id(target_id)
    if target_seat is None or not target_seat.alive:
        return DoctorProtectResult(protected=None, reason=REASON_INVALID_TARGET)

    if target_id == doctor.last_protected_target:
        return DoctorProtectResult(protected=None, reason=REASON_REPEAT_VIOLATION)

    return DoctorProtectResult(protected=target_id, reason=REASON_PROTECTED)
