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
    CleanSubmitted,
    DayVoteResolved,
    DetectiveResultDelivered,
    Event,
    FrameSubmitted,
    GameCreated,
    GameTerminated,
    InvestigateSubmitted,
    MafiaKillVoteSubmitted,
    NightFeedbackDelivered,
    NightResolved,
    OutputInvalid,
    OutputTruncated,
    PhaseResolved,
    PhaseStarted,
    PlayerEliminated,
    PrivateMessageSubmitted,
    ProtectSubmitted,
    PublicMessageSubmitted,
    RoleblockSubmitted,
    RolesAssigned,
    SeatTakenOver,
    TrackSubmitted,
    VoteSubmitted,
    WatchSubmitted,
)
from padrino.core.engine.state import (
    GameState,
    Phase,
    QueuedInspection,
    Seat,
    framer_frame_shots_remaining,
    janitor_clean_shots_remaining,
)
from padrino.core.enums import PhaseKind, Role

# Event classes that are recorded in the log but do not mutate mechanical
# state. Chat events are part of this set per the chat-vs-action firewall.
_RECORDED_ONLY: tuple[type[Event], ...] = (
    PublicMessageSubmitted,
    PrivateMessageSubmitted,
    VoteSubmitted,
    MafiaKillVoteSubmitted,
    InvestigateSubmitted,
    RoleblockSubmitted,
    FrameSubmitted,
    TrackSubmitted,
    WatchSubmitted,
    CleanSubmitted,
    NightFeedbackDelivered,
    ActionTimedOut,
    OutputTruncated,
    OutputInvalid,
    DayVoteResolved,
    NightResolved,
    PhaseResolved,
    SeatTakenOver,
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


def _spend_janitor_clean_shots(state: GameState, actor_ids: tuple[str, ...]) -> GameState:
    if not actor_ids:
        return state
    spent = set(actor_ids)
    changed = False
    seats: list[Seat] = []
    for seat in state.seats:
        if seat.public_player_id in spent and seat.role is Role.JANITOR:
            remaining = max(janitor_clean_shots_remaining(seat) - 1, 0)
            seats.append(seat.model_copy(update={"janitor_clean_shots_remaining": remaining}))
            changed = True
        else:
            seats.append(seat)
    if not changed:
        return state
    return state.model_copy(update={"seats": tuple(seats)})


def _spend_framer_frame_shots(state: GameState, actor_ids: tuple[str, ...]) -> GameState:
    if not actor_ids:
        return state
    spent = set(actor_ids)
    changed = False
    seats: list[Seat] = []
    for seat in state.seats:
        if seat.public_player_id in spent and seat.role is Role.FRAMER:
            remaining = max(framer_frame_shots_remaining(seat) - 1, 0)
            seats.append(seat.model_copy(update={"framer_frame_shots_remaining": remaining}))
            changed = True
        else:
            seats.append(seat)
    if not changed:
        return state
    return state.model_copy(update={"seats": tuple(seats)})


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
                seat_kind=a.seat_kind,
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
    if isinstance(event, NightResolved):
        state = _spend_janitor_clean_shots(state, event.payload.clean_spent_actor_ids)
        return _spend_framer_frame_shots(state, event.payload.frame_spent_actor_ids)
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


def compute_seat_provenance(event_log: list[Event]) -> dict[str, str]:
    """Derive each seat's occupancy provenance from the event log.

    Returns a mapping ``{public_player_id: 'HUMAN'|'AI'|'HUMAN_THEN_AI'}`` built
    purely from the events. Seats are first labelled by their assignment-time
    :class:`~padrino.core.engine.state.SeatKind` (``HUMAN``/``AI_TAKEOVER`` map to
    ``HUMAN``/``AI`` respectively; an absent ``seat_kind`` defaults to ``AI`` so a
    legacy AI-only log is unchanged). Every :class:`SeatTakenOver` upgrades a
    ``HUMAN`` seat to ``HUMAN_THEN_AI``; an already-AI seat stays ``AI``.

    This is pure data: no clock, no random, no IO.
    """
    provenance: dict[str, str] = {}
    for event in event_log:
        if isinstance(event, RolesAssigned):
            for assignment in event.payload.assignments:
                kind = assignment.seat_kind
                provenance[assignment.public_player_id] = "HUMAN" if kind == "HUMAN" else "AI"
        elif isinstance(event, SeatTakenOver):
            pid = event.payload.public_player_id
            if provenance.get(pid) == "HUMAN":
                provenance[pid] = "HUMAN_THEN_AI"
            else:
                provenance.setdefault(pid, "AI")
    return provenance


__all__ = ["apply_event", "compute_seat_provenance", "initial_state"]
