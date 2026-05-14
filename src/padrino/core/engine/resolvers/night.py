"""Night resolution composer.

Pure function: given a `GameState` and the full mapping of seat submissions for
the night, dispatch to the mafia-kill, doctor-protect, and detective-investigate
resolvers and compose the resulting deaths and queued findings.

Composition rules:
- Doctor protect cancels a mafia kill only when the protected seat equals the
  kill target.
- A detective finding is suppressed if the detective is eliminated during the
  same night resolution.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from padrino.core.engine.actions import Action
from padrino.core.engine.resolvers.detective import resolve_detective_investigation
from padrino.core.engine.resolvers.doctor_protect import resolve_doctor_protect
from padrino.core.engine.resolvers.mafia_kill import resolve_mafia_kill
from padrino.core.engine.state import GameState
from padrino.core.enums import Faction, Role


class NightResolution(BaseModel):
    """Composed outcome of a single night phase. Immutable."""

    model_config = ConfigDict(frozen=True)

    eliminated: str | None
    protected: str | None
    detective_finding: tuple[str, str] | None
    mafia_kill_target: str | None


def resolve_night(
    state: GameState,
    all_submissions: Mapping[str, Action],
) -> NightResolution:
    """Compose mafia-kill, doctor-protect, and detective-investigate results."""
    mafia_submissions: dict[str, Action] = {}
    for seat in state.seats:
        if seat.faction is not Faction.MAFIA:
            continue
        action = all_submissions.get(seat.public_player_id)
        if action is not None:
            mafia_submissions[seat.public_player_id] = action

    doctor_seat = next((s for s in state.seats if s.role is Role.DOCTOR), None)
    doctor_submission = (
        all_submissions.get(doctor_seat.public_player_id) if doctor_seat is not None else None
    )

    detective_seat = next((s for s in state.seats if s.role is Role.DETECTIVE), None)
    detective_submission = (
        all_submissions.get(detective_seat.public_player_id) if detective_seat is not None else None
    )

    kill_result = resolve_mafia_kill(state, mafia_submissions)
    protect_result = resolve_doctor_protect(state, doctor_submission)
    invest_result = resolve_detective_investigation(state, detective_submission)

    mafia_kill_target = kill_result.target
    protected = protect_result.protected

    if mafia_kill_target is None or mafia_kill_target == protected:
        eliminated: str | None = None
    else:
        eliminated = mafia_kill_target

    detective_finding: tuple[str, str] | None = None
    if (
        invest_result.target is not None
        and invest_result.finding is not None
        and detective_seat is not None
        and eliminated != detective_seat.public_player_id
    ):
        detective_finding = (invest_result.target, invest_result.finding)

    return NightResolution(
        eliminated=eliminated,
        protected=protected,
        detective_finding=detective_finding,
        mafia_kill_target=mafia_kill_target,
    )
