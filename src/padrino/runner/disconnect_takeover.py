"""Silent AI takeover of a disconnected human seat (US-150).

When a human seat's reconnect grace window expires
(:func:`padrino.core.disconnect.seats_past_grace`), a curated AI silently
assumes the seat so the game continues — invisibly in anonymous mode. This
impure runner helper performs the swap atomically with recording the canonical
provenance:

1. Swap the seat's adapter on the EXISTING :class:`SeatMultiplexAdapter`
   (US-139's :meth:`~padrino.llm.multiplex.SeatMultiplexAdapter.swap_seat`),
   between ticks, for the curated replacement adapter.
2. Append a single canonical :class:`~padrino.core.engine.events.SeatTakenOver`
   event (US-122) to the hash-chained log. The payload is pure data — the
   logical ``day`` / ``phase`` come from the engine state, never a wall clock —
   so the change is replay-reconstructable and
   :func:`~padrino.core.engine.reducer.compute_seat_provenance` derives
   ``HUMAN_THEN_AI`` for the seat at the endgame reveal.

The takeover is **reveal-only**: the ``SeatTakenOver`` event is SYSTEM-visibility
(never on a public/live frame), so it stays invisible mid-game and surfaces only
in the terminal reveal provenance. Folding the event preserves all mechanical
state, so the swap never perturbs deterministic replay.

This module lives in the impure runner layer; the purity-firewall test scans
only ``game_runner.py``, so a sibling runner module may orchestrate the swap.
The pure decision of WHEN to take over lives in :mod:`padrino.core.disconnect`.
"""

from __future__ import annotations

from dataclasses import dataclass

from padrino.core.engine.event_log import EventLog, StoredEvent
from padrino.core.engine.state import GameState
from padrino.core.observations import format_phase_id
from padrino.llm.adapter import LlmAdapter
from padrino.llm.multiplex import SeatMultiplexAdapter

_DEFAULT_REASON = "disconnect_grace_expired"


@dataclass(frozen=True, slots=True)
class TakeoverResult:
    """Outcome of one silent AI takeover.

    ``event`` is the committed :class:`SeatTakenOver` envelope (provenance-only).
    ``replaced_adapter`` is the adapter the seat held before the swap — the human
    seat's adapter — returned so the caller can dispose of it.
    """

    event: StoredEvent
    replaced_adapter: LlmAdapter


def take_over_seat(
    *,
    mux: SeatMultiplexAdapter,
    event_log: EventLog,
    state: GameState,
    seat_id: str,
    replacement_adapter: LlmAdapter,
    replacement_agent_build_ref: str,
    reason: str = _DEFAULT_REASON,
) -> TakeoverResult:
    """Silently swap ``seat_id`` to ``replacement_adapter`` and record provenance.

    Rebinds the seat's adapter on ``mux`` (between ticks) and appends a single
    :class:`SeatTakenOver` event to ``event_log``. The event's ``day`` / ``phase``
    are read from ``state.current_phase`` — pure logical values, never a clock —
    so the resulting log replays bit-for-bit and reconstructs ``HUMAN_THEN_AI``
    provenance for the seat. Raises ``KeyError`` (via ``swap_seat``) if the seat
    is unknown to the multiplex (a takeover replaces an existing occupant, never
    introduces a seat).
    """
    replaced = mux.swap_seat(seat_id, replacement_adapter)
    phase_id = format_phase_id(state.current_phase)
    event = event_log.append(
        {
            "event_type": "SeatTakenOver",
            "sequence": len(event_log.events),
            "phase": phase_id,
            "visibility": "SYSTEM",
            "actor_player_id": None,
            "payload": {
                "public_player_id": seat_id,
                "day": state.current_phase.day,
                "phase": phase_id,
                "reason": reason,
                "replacement_agent_build_ref": replacement_agent_build_ref,
            },
        }
    )
    return TakeoverResult(event=event, replaced_adapter=replaced)


__all__ = ["TakeoverResult", "take_over_seat"]
