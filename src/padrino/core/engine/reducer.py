"""Pure event reducer: ``(state, event) -> state``.

Folding the full event log through :func:`apply_event` reconstructs the final
:class:`GameState`. The reducer is the shared transition layer between live
execution (US-016 / US-032) and replay (US-018) — both produce the same final
state from the same events.

The reducer never mutates its input state. Every branch returns a new frozen
:class:`GameState` via ``model_copy``.

Chat firewall: ``PublicMessageSubmitted`` and ``PrivateMessageSubmitted`` are
recorded but have no mechanical effect — chat is never parsed for game logic.
"""

from __future__ import annotations

from padrino.core.engine.events import (
    ActionTimedOut,
    DayVoteResolved,
    DetectiveResultDelivered,
    Event,
    GameCreated,
    GameTerminated,
    InvestigateSubmitted,
    MafiaKillVoteSubmitted,
    NightResolved,
    OutputInvalid,
    OutputTruncated,
    PhaseResolved,
    PhaseStarted,
    PlayerEliminated,
    PrivateMessageSubmitted,
    ProtectSubmitted,
    PublicMessageSubmitted,
    RolesAssigned,
    VoteSubmitted,
)
from padrino.core.engine.state import GameState, Phase, QueuedInspection, Seat
from padrino.core.enums import PhaseKind

# Event classes that are recorded in the log but do not mutate mechanical
# state. Chat events are part of this set per the chat-vs-action firewall.
_RECORDED_ONLY: tuple[type[Event], ...] = (
    PublicMessageSubmitted,
    PrivateMessageSubmitted,
    VoteSubmitted,
    MafiaKillVoteSubmitted,
    InvestigateSubmitted,
    ActionTimedOut,
    OutputTruncated,
    OutputInvalid,
    DayVoteResolved,
    NightResolved,
    PhaseResolved,
)


def initial_state() -> GameState:
    """Return an empty pre-game state, suitable as the seed for replay folding."""
    return GameState(
        ruleset_id="",
        game_id="",
        game_seed="",
        current_phase=Phase(kind=PhaseKind.SETUP, day=0, round=0),
        seats=(),
        day=0,
    )


def _update_seat(state: GameState, public_player_id: str, updates: dict[str, object]) -> GameState:
    new_seats = tuple(
        s.model_copy(update=updates) if s.public_player_id == public_player_id else s
        for s in state.seats
    )
    return state.model_copy(update={"seats": new_seats})


def apply_event(state: GameState, event: Event) -> GameState:
    """Return the next :class:`GameState` after applying ``event``.

    Raises :class:`ValueError` for any event type not in the known catalogue.
    """
    if isinstance(event, GameCreated):
        return state.model_copy(
            update={
                "ruleset_id": event.payload.ruleset_id,
                "game_id": event.payload.game_id,
                "game_seed": event.payload.game_seed,
            }
        )
    if isinstance(event, RolesAssigned):
        seats = tuple(
            Seat(
                public_player_id=a.public_player_id,
                seat_index=a.seat_index,
                role=a.role,
                faction=a.faction,
                alive=True,
            )
            for a in event.payload.assignments
        )
        return state.model_copy(update={"seats": seats})
    if isinstance(event, PhaseStarted):
        new_phase = Phase(
            kind=PhaseKind(event.payload.phase_kind),
            day=event.payload.day,
            round=event.payload.round,
        )
        return state.model_copy(update={"current_phase": new_phase, "day": event.payload.day})
    if isinstance(event, PlayerEliminated):
        return _update_seat(
            state,
            event.payload.public_player_id,
            {"alive": False, "death_phase": event.phase},
        )
    if isinstance(event, DetectiveResultDelivered):
        queued = QueuedInspection(target=event.payload.target, finding=event.payload.finding)
        return _update_seat(state, event.actor_player_id, {"queued_inspection_result": queued})
    if isinstance(event, ProtectSubmitted):
        return _update_seat(
            state, event.actor_player_id, {"last_protected_target": event.payload.target}
        )
    if isinstance(event, GameTerminated):
        return state.model_copy(
            update={
                "terminal_result": event.payload.winner,
                "terminal_reason": event.payload.reason,
            }
        )
    if isinstance(event, _RECORDED_ONLY):
        return state
    raise ValueError(f"unknown event type: {type(event).__name__}")


__all__ = ["apply_event", "initial_state"]
