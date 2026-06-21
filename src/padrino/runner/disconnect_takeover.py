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
from padrino.core.engine.hashing import compute_event_hash
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


def build_takeover_event(
    *,
    event_log: EventLog,
    state: GameState,
    seat_id: str,
    replacement_agent_build_ref: str,
    reason: str = _DEFAULT_REASON,
) -> StoredEvent:
    """Build (but do NOT append) the canonical SeatTakenOver envelope (US-197).

    Computes the sealed :class:`StoredEvent` that *would* be appended to
    ``event_log`` for a takeover of ``seat_id``, chaining from the log's current
    ``head_hash`` at the next contiguous sequence. The log is NOT mutated — this
    is the pure "envelope build" half that lets the runner persist+commit the
    paired DB row FIRST and apply to the in-memory log/mux only AFTER the commit
    succeeds (US-197 AC1), so the in-memory log never advances past what is
    durably committed in ``game_events``.

    The event's ``day`` / ``phase`` are read from ``state.current_phase`` — pure
    logical values, never a clock — so the resulting log replays bit-for-bit and
    reconstructs ``HUMAN_THEN_AI`` provenance for the seat.
    """
    phase_id = format_phase_id(state.current_phase)
    sequence = len(event_log.events)
    body = {
        "event_type": "SeatTakenOver",
        "sequence": sequence,
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
    prev_event_hash = event_log.head_hash
    event_hash = compute_event_hash(prev_event_hash, body)
    return StoredEvent(
        sequence=sequence,
        prev_event_hash=prev_event_hash,
        event_hash=event_hash,
        body=body,
    )


def apply_takeover(
    *,
    mux: SeatMultiplexAdapter,
    event_log: EventLog,
    event: StoredEvent,
    seat_id: str,
    replacement_adapter: LlmAdapter,
) -> TakeoverResult:
    """Apply a pre-built, already-committed takeover to the in-memory state (US-197).

    The in-memory commit half: rebind the seat's adapter on ``mux`` (between
    ticks) and append the pre-built, durably-committed ``event`` to
    ``event_log``. Call this ONLY AFTER the paired DB write (seat mutation +
    SeatTakenOver row) has committed, so the in-memory log/mux never advance past
    what is in ``game_events`` (US-197 AC1).

    ``event`` must seal contiguously onto the current log head; a mismatch raises
    :class:`ValueError` rather than silently forking the chain. Raises
    ``KeyError`` (via ``swap_seat``) if the seat is unknown to the multiplex.
    """
    expected_sequence = len(event_log.events)
    if event.sequence != expected_sequence or event.prev_event_hash != event_log.head_hash:
        raise ValueError(
            f"takeover event for seat {seat_id!r} does not seal onto the current log head "
            f"(expected sequence {expected_sequence} chaining from {event_log.head_hash!r})"
        )
    replaced = mux.swap_seat(seat_id, replacement_adapter)
    appended = event_log.append(event.body)
    return TakeoverResult(event=appended, replaced_adapter=replaced)


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
    """Build and immediately apply a takeover to the in-memory state.

    Convenience wrapper combining :func:`build_takeover_event` and
    :func:`apply_takeover` for callers that do NOT need to interleave a durable
    DB commit between the envelope build and the in-memory apply. The production
    human lane uses the two halves separately so it can persist+commit the paired
    ``game_events`` row BEFORE mutating the long-lived in-memory log/mux (US-197);
    do not use this wrapper there.
    """
    event = build_takeover_event(
        event_log=event_log,
        state=state,
        seat_id=seat_id,
        replacement_agent_build_ref=replacement_agent_build_ref,
        reason=reason,
    )
    return apply_takeover(
        mux=mux,
        event_log=event_log,
        event=event,
        seat_id=seat_id,
        replacement_adapter=replacement_adapter,
    )


__all__ = ["TakeoverResult", "apply_takeover", "build_takeover_event", "take_over_seat"]
