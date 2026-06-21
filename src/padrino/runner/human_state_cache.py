"""Snapshot-backed current-state resolver for human request paths (US-168).

Human action/chat/observation requests need the current deterministic state, but
minutes-long games can have large event logs. The production runner already
writes a durable ``human_game_runtime`` row every phase transition and tick; this
module stores a cache of the folded :class:`GameState` plus the ref-only
hash-chain envelopes at that snapshot head. Request paths can then validate the
cached head against the DB and read only events committed after it.

The cache never stores raw human chat. Human chat events in the hash chain carry
only ``text=""`` plus ``content_ref``; raw/cleaned human text remains in the
hold/sidecar tables.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.engine.event_log import EventLog, StoredEvent
from padrino.core.engine.events import EventAdapter
from padrino.core.engine.reducer import apply_event
from padrino.core.engine.replay import ReplayHashMismatchError
from padrino.core.engine.state import GameState
from padrino.db.models import GameEvent
from padrino.db.repositories import events as events_repo
from padrino.db.repositories import human_game_runtime as runtime_repo
from padrino.runner.human_durability import replay_state_from_rows

STATE_CACHE_VERSION = 1


@dataclass(frozen=True, slots=True)
class ResolvedHumanState:
    """Current state/log resolved for one human request."""

    state: GameState
    event_log: EventLog
    used_cache: bool
    incremental_event_count: int


@dataclass(frozen=True, slots=True)
class _ParsedStateCache:
    state: GameState
    event_log: EventLog
    sequence_head: int
    event_hash_head: str


def build_state_cache(state: GameState, event_log: EventLog) -> dict[str, Any]:
    """Serialize a runtime state cache for ``human_game_runtime.state_cache``."""
    events = event_log.events
    sequence_head = events[-1].sequence if events else -1
    return {
        "version": STATE_CACHE_VERSION,
        "sequence_head": sequence_head,
        "event_hash_head": event_log.head_hash,
        "state": state.model_dump(mode="json"),
        "event_log": [stored.model_dump(mode="json") for stored in events],
    }


async def resolve_current_human_state(
    session: AsyncSession,
    game_id: uuid.UUID,
) -> ResolvedHumanState | None:
    """Resolve current ``(GameState, EventLog)`` using the runtime cache if valid.

    Returns ``None`` when the game has no events. The fallback path preserves the
    older full replay behavior for legacy rows and stale snapshots.
    """
    runtime = await runtime_repo.get(session, game_id)
    cached = _parse_state_cache(runtime.state_cache if runtime is not None else None)
    if cached is not None and await _cache_head_matches_db(session, game_id, cached):
        try:
            suffix = await events_repo.list_events_after(
                session,
                game_id,
                after_sequence=cached.sequence_head,
            )
            state, event_log = _apply_suffix(cached.state, cached.event_log, suffix)
            return ResolvedHumanState(
                state=state,
                event_log=event_log,
                used_cache=True,
                incremental_event_count=len(suffix),
            )
        except (ReplayHashMismatchError, ValueError):
            # A stale/corrupt cache must not compromise correctness. Fall back to
            # the existing verified replay path, which re-seals the DB log.
            pass

    rows = await events_repo.list_events(session, game_id)
    if not rows:
        return None
    state, event_log = replay_state_from_rows(rows)
    return ResolvedHumanState(
        state=state,
        event_log=event_log,
        used_cache=False,
        incremental_event_count=len(rows),
    )


def _parse_state_cache(raw: object) -> _ParsedStateCache | None:
    if not isinstance(raw, Mapping):
        return None
    if raw.get("version") != STATE_CACHE_VERSION:
        return None
    sequence_head = raw.get("sequence_head")
    event_hash_head = raw.get("event_hash_head")
    state_raw = raw.get("state")
    event_log_raw = raw.get("event_log")
    if not isinstance(sequence_head, int):
        return None
    if not isinstance(event_hash_head, str):
        return None
    if not isinstance(state_raw, Mapping):
        return None
    if not isinstance(event_log_raw, list):
        return None

    try:
        stored_events = tuple(StoredEvent.model_validate(item) for item in event_log_raw)
        event_log = EventLog.from_stored(stored_events)
        state = GameState.model_validate(state_raw)
    except (TypeError, ValueError):
        return None

    if sequence_head != len(stored_events) - 1:
        return None
    if event_log.head_hash != event_hash_head:
        return None
    return _ParsedStateCache(
        state=state,
        event_log=event_log,
        sequence_head=sequence_head,
        event_hash_head=event_hash_head,
    )


async def _cache_head_matches_db(
    session: AsyncSession,
    game_id: uuid.UUID,
    cached: _ParsedStateCache,
) -> bool:
    if cached.sequence_head < 0:
        return False
    row = await events_repo.get_event_at_sequence(
        session,
        game_id,
        sequence=cached.sequence_head,
    )
    return row is not None and row.event_hash == cached.event_hash_head


def _event_body(row: GameEvent) -> dict[str, object]:
    return {
        "event_type": row.event_type,
        "sequence": row.sequence,
        "phase": row.phase,
        "visibility": row.visibility,
        "actor_player_id": row.actor_player_id,
        "payload": row.payload,
    }


def _apply_suffix(
    state: GameState,
    event_log: EventLog,
    rows: list[GameEvent],
) -> tuple[GameState, EventLog]:
    current_state = state
    current_log = event_log
    for row in rows:
        body = _event_body(row)
        stored = current_log.append(body)
        if stored.sequence != row.sequence:
            raise ValueError(f"event suffix sequence {row.sequence} does not follow cached head")
        if stored.prev_event_hash != row.prev_event_hash:
            raise ReplayHashMismatchError(
                row.sequence,
                row.prev_event_hash,
                stored.prev_event_hash,
            )
        if stored.event_hash != row.event_hash:
            raise ReplayHashMismatchError(row.sequence, row.event_hash, stored.event_hash)
        current_state = apply_event(current_state, EventAdapter.validate_python(body))
    return current_state, current_log


__all__ = [
    "STATE_CACHE_VERSION",
    "ResolvedHumanState",
    "build_state_cache",
    "resolve_current_human_state",
]
