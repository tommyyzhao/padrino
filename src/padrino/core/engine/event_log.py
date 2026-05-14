"""In-memory hash-chained event log.

Wraps an opaque event body in a tamper-evident envelope: contiguous sequence
numbers from zero, ``prev_event_hash`` chained from :data:`GENESIS_HASH`, and
``event_hash`` computed via :func:`compute_event_hash`. Pure — no DB, no clock,
no network. Any ``created_at`` value lives inside the body and is excluded from
hashing by :mod:`padrino.core.engine.hashing`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict

from padrino.core.engine.hashing import GENESIS_HASH, compute_event_hash


class StoredEvent(BaseModel):
    """An event body sealed with sequence number and hash-chain envelope."""

    model_config = ConfigDict(frozen=True)

    sequence: int
    prev_event_hash: str
    event_hash: str
    body: dict[str, Any]


class EventLog:
    """Append-only hash-chained log over arbitrary event-body dicts."""

    __slots__ = ("_events",)

    def __init__(self) -> None:
        self._events: list[StoredEvent] = []

    @property
    def head_hash(self) -> str:
        """Hash of the most recent event, or :data:`GENESIS_HASH` if empty."""
        if not self._events:
            return GENESIS_HASH
        return self._events[-1].event_hash

    @property
    def events(self) -> tuple[StoredEvent, ...]:
        """Immutable snapshot of all stored events in append order."""
        return tuple(self._events)

    def append(self, event_body: Mapping[str, Any]) -> StoredEvent:
        """Seal ``event_body`` into the chain and return the StoredEvent."""
        sequence = len(self._events)
        prev_event_hash = self.head_hash
        event_hash = compute_event_hash(prev_event_hash, event_body)
        stored = StoredEvent(
            sequence=sequence,
            prev_event_hash=prev_event_hash,
            event_hash=event_hash,
            body=dict(event_body),
        )
        self._events.append(stored)
        return stored


__all__ = ["EventLog", "StoredEvent"]
