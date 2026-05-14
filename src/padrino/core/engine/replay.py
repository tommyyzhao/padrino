"""Deterministic replay primitives.

Two pure entry points, both depending only on already-shipped pure-core modules:

- :func:`replay_events` folds a typed :class:`Event` sequence through the
  reducer to reconstruct the final :class:`GameState`. This is the "engine
  replay" mode — given the events, recover the state.

- :func:`replay_event_log` re-appends the bodies of prior
  :class:`StoredEvent` records to a fresh :class:`EventLog`, verifying that
  the regenerated chain reproduces every original sequence number and
  ``event_hash``. ``created_at`` is excluded from the hash by construction,
  so replay with a different clock value still verifies.

The LLM frozen-response replay (consume archived ``AgentResponse`` objects
through the engine loop to regenerate the event log) is intentionally not in
this module: it depends on US-021 (AgentResponse) and US-029 (GameRunner /
GameConfig), which have not yet shipped.
"""

from __future__ import annotations

from collections.abc import Sequence

from padrino.core.engine.event_log import EventLog, StoredEvent
from padrino.core.engine.events import Event
from padrino.core.engine.reducer import apply_event, initial_state
from padrino.core.engine.state import GameState


class ReplayHashMismatchError(ValueError):
    """Raised when a replayed event's hash disagrees with the stored hash.

    Attribute ``sequence`` carries the sequence number of the first event
    whose recomputed ``event_hash`` did not match the stored value.
    """

    def __init__(self, sequence: int, expected: str, actual: str) -> None:
        super().__init__(
            f"replay hash mismatch at sequence {sequence}: expected {expected}, got {actual}"
        )
        self.sequence = sequence
        self.expected = expected
        self.actual = actual


def replay_events(events: Sequence[Event]) -> GameState:
    """Fold ``events`` through :func:`apply_event` and return the final state."""
    state = initial_state()
    for event in events:
        state = apply_event(state, event)
    return state


def replay_event_log(stored_events: Sequence[StoredEvent]) -> EventLog:
    """Re-seal each prior body through a fresh :class:`EventLog` and verify."""
    log = EventLog()
    for prior in stored_events:
        replayed = log.append(prior.body)
        if replayed.event_hash != prior.event_hash:
            raise ReplayHashMismatchError(
                sequence=prior.sequence,
                expected=prior.event_hash,
                actual=replayed.event_hash,
            )
    return log


__all__ = ["ReplayHashMismatchError", "replay_event_log", "replay_events"]
