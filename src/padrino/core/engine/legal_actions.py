"""Legal action computation per seat and phase.

`legal_actions_for(state, seat)` is the single source of truth for which
`ActionType` values a player may submit and which target seat ids are legal
for those actions. Used both by the observation builder (US-019) and by the
response validator (US-021/US-023) to ensure they share one ruleset reading.

Pure function. Reads `GameState.current_phase` plus the seat's role / faction /
alive flag / `last_protected_target` and produces a `LegalActions` snapshot.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from padrino.core.engine.state import GameState, Seat, janitor_clean_shots_remaining
from padrino.core.enums import ActionType, Faction, PhaseKind, Role


class LegalActions(BaseModel):
    """Snapshot of legal action types and targets for one seat in one phase."""

    model_config = ConfigDict(frozen=True)

    allowed_action_types: list[ActionType]
    legal_targets: list[str]


_EMPTY = LegalActions(allowed_action_types=[], legal_targets=[])
_NOOP_ONLY = LegalActions(allowed_action_types=[ActionType.NOOP], legal_targets=[])
TARGETED_ACTION_TYPES = frozenset(
    {
        ActionType.VOTE,
        ActionType.MAFIA_KILL,
        ActionType.PROTECT,
        ActionType.INVESTIGATE,
        ActionType.ROLEBLOCK,
        ActionType.FRAME,
        ActionType.TRACK,
        ActionType.WATCH,
        ActionType.CLEAN,
    }
)

_FUTURE_NIGHT_ROLE_ACTIONS: dict[Role, ActionType] = {
    Role.MAFIA_ROLEBLOCKER: ActionType.ROLEBLOCK,
    Role.FRAMER: ActionType.FRAME,
    Role.TRACKER: ActionType.TRACK,
    Role.WATCHER: ActionType.WATCH,
    Role.JANITOR: ActionType.CLEAN,
}
_FACTIONAL_KILL_ROLES = frozenset({Role.MAFIA_GOON, Role.GODFATHER})


def action_requires_target(action_type: ActionType) -> bool:
    """Return whether ``action_type`` must name a target from ``legal_targets``."""

    return action_type in TARGETED_ACTION_TYPES


def _living_others(state: GameState, seat: Seat) -> list[str]:
    return [
        s.public_player_id
        for s in state.living_seats()
        if s.public_player_id != seat.public_player_id
    ]


def legal_actions_for(state: GameState, seat: Seat) -> LegalActions:
    """Return the legal action types and targets for `seat` in `state.current_phase`."""
    if not seat.alive:
        return _EMPTY

    kind = state.current_phase.kind

    if kind is PhaseKind.DAY_DISCUSSION:
        return _NOOP_ONLY

    if kind is PhaseKind.DAY_VOTE:
        targets = [
            s.public_player_id
            for s in state.living_seats()
            if s.public_player_id != seat.public_player_id
        ]
        return LegalActions(
            allowed_action_types=[ActionType.VOTE, ActionType.ABSTAIN],
            legal_targets=targets,
        )

    if kind is PhaseKind.NIGHT_0_MAFIA_INTRO or kind is PhaseKind.NIGHT_MAFIA_DISCUSSION:
        if seat.faction is Faction.MAFIA:
            return _NOOP_ONLY
        return _EMPTY

    if kind is PhaseKind.NIGHT_ACTIONS:
        if seat.role in _FACTIONAL_KILL_ROLES:
            targets = [
                s.public_player_id for s in state.living_seats() if s.faction is not Faction.MAFIA
            ]
            return LegalActions(
                allowed_action_types=[ActionType.MAFIA_KILL],
                legal_targets=targets,
            )
        if seat.role is Role.DOCTOR:
            targets = [
                s.public_player_id
                for s in state.living_seats()
                if s.public_player_id != seat.last_protected_target
            ]
            return LegalActions(
                allowed_action_types=[ActionType.PROTECT],
                legal_targets=targets,
            )
        if seat.role is Role.DETECTIVE:
            targets = [
                s.public_player_id
                for s in state.living_seats()
                if s.public_player_id != seat.public_player_id
            ]
            return LegalActions(
                allowed_action_types=[ActionType.INVESTIGATE],
                legal_targets=targets,
            )
        if seat.role in _FUTURE_NIGHT_ROLE_ACTIONS:
            if seat.role is Role.JANITOR and janitor_clean_shots_remaining(seat) <= 0:
                return _NOOP_ONLY
            return LegalActions(
                allowed_action_types=[_FUTURE_NIGHT_ROLE_ACTIONS[seat.role]],
                legal_targets=_living_others(state, seat),
            )
        return _NOOP_ONLY

    return _EMPTY


__all__ = [
    "TARGETED_ACTION_TYPES",
    "LegalActions",
    "action_requires_target",
    "legal_actions_for",
]
