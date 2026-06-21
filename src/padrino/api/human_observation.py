"""Per-seat live observation stream + phase-deadline frame (US-136).

A human player's client needs two things its spectator-facing siblings never
deliver:

* its *own* seat observation — the private information that seat is entitled to
  see (its private events, its role/faction, mafia teammates, detective history)
  plus the legal actions available this phase; and
* the current **phase deadline** — the wall-clock instant by which the seat must
  act, so a human UI can render a countdown.

This impure shell:

* resolves the caller's seat from ``occupant_principal_id`` (a human may only
  observe the seat they occupy — 403 otherwise);
* resolves the deterministic :class:`~padrino.core.engine.state.GameState` and
  :class:`~padrino.core.engine.event_log.EventLog` from the durable human runtime
  cache, reading only events committed after the cached head when possible, then
  reuses the PURE :func:`padrino.core.observations.build_observation` to project
  that seat's view (no new branching in core);
* reads the phase deadline from the impure ``human_game_runtime`` row — the
  deadline is **transport-only** and is NEVER written to the hash-chained log
  (hard rule 4: a wall-clock value never enters a hashed event);
* in anonymous mode, asserts the observation carries no human-vs-AI / model
  identity markers (:func:`assert_no_identity_markers`).

The frames are emitted as Server-Sent Events: each carries a ``type``
discriminator (``observation`` / ``phase_deadline``) so a client can route them.
All wall-clock pacing lives here in the impure shell; the core stays pure.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.observation_privacy import (
    assert_no_identity_markers,
    coerce_identity_mode,
    is_anonymous,
)
from padrino.core.observations import build_observation, format_phase_id
from padrino.core.rulesets import get_ruleset
from padrino.db.models import Game, GameSeat
from padrino.db.repositories import human_game_runtime as runtime_repo
from padrino.db.repositories import human_seat_presence as presence_repo
from padrino.runner.human_chat_observation import (
    hydrate_observation_human_chat,
    load_released_human_chat_texts,
)
from padrino.runner.human_state_cache import resolve_current_human_state

GAME_NOT_FOUND_DETAIL = "game_not_found"
WRONG_SEAT_DETAIL = "wrong_seat"

#: SSE frame discriminators.
OBSERVATION_FRAME = "observation"
DEADLINE_FRAME = "phase_deadline"


@dataclass(frozen=True, slots=True)
class SeatObservationSnapshot:
    """One point-in-time render of a seat's stream: its observation + deadline."""

    public_player_id: str
    phase: str
    identity_mode: str
    observation_frame: dict[str, Any]
    deadline_frame: dict[str, Any]


async def _resolve_seat(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    principal_id: uuid.UUID,
) -> GameSeat:
    """Return the seat the principal occupies in this game, or 403.

    A human may observe ONLY the seat they occupy. A request for a game the
    principal has no seat in (or any other seat) is a wrong-seat rejection — the
    seat-scoped stream never leaks another seat's private view.
    """
    stmt = select(GameSeat).where(
        GameSeat.game_id == game_id,
        GameSeat.occupant_principal_id == principal_id,
    )
    seat = (await session.execute(stmt)).scalar_one_or_none()
    if seat is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=WRONG_SEAT_DETAIL)
    return seat


def _as_aware(value: datetime | None) -> datetime | None:
    """Coerce a (possibly tz-naive) stored deadline back to UTC-aware.

    SQLite drops ``tzinfo`` from a ``DateTime(timezone=True)`` column, so a
    deadline persisted as UTC-aware loads back naive. Coercing it here keeps the
    emitted ISO-8601 deadline identical to what the runner persisted.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _deadline_frame(phase: str, deadline_at: datetime | None) -> dict[str, Any]:
    """Build the transport-only phase-deadline frame.

    Carries the wall-clock deadline as an ISO-8601 string (or ``None`` when no
    deadline is set). This frame is emitted over the wire ONLY — it is never
    written to the hash-chained event log (hard rule 4).
    """
    return {
        "type": DEADLINE_FRAME,
        "phase": phase,
        "deadline_at": deadline_at.isoformat() if deadline_at is not None else None,
    }


async def build_seat_observation_snapshot(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    principal_id: uuid.UUID,
) -> SeatObservationSnapshot:
    """Render the caller's seat observation + the current phase-deadline frame.

    Raises :class:`fastapi.HTTPException` for a wrong-seat request (403) or an
    unknown game (404). Reuses the pure :func:`build_observation`; in anonymous
    mode the observation is asserted free of identity markers.
    """
    seat_row = await _resolve_seat(session, game_id=game_id, principal_id=principal_id)
    await presence_repo.record_heartbeat(
        session,
        game_id=game_id,
        public_player_id=seat_row.public_player_id,
        seen_at=datetime.now(UTC),
    )

    game = await session.get(Game, game_id)
    if game is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GAME_NOT_FOUND_DETAIL)

    resolved = await resolve_current_human_state(session, game_id)
    if resolved is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GAME_NOT_FOUND_DETAIL)

    state = resolved.state
    event_log = resolved.event_log
    core_seat = state.seat_by_public_id(seat_row.public_player_id)
    if core_seat is None:
        # The seat is in the DB but not yet in the replayed state — treat as
        # wrong seat rather than leaking an empty/foreign view.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=WRONG_SEAT_DETAIL)

    ruleset = get_ruleset(game.ruleset_id)
    observation = build_observation(state, core_seat, event_log, ruleset)
    texts_by_sequence = await load_released_human_chat_texts(session, game_id=game_id)
    observation = hydrate_observation_human_chat(observation, texts_by_sequence)

    identity_mode = coerce_identity_mode(game.identity_mode)
    observation_payload = observation.model_dump(mode="json")
    observation_frame: dict[str, Any] = {"type": OBSERVATION_FRAME, **observation_payload}

    if is_anonymous(identity_mode):
        # The per-seat view carries the seat's OWN role/faction (allowed) but
        # must never carry a human-vs-AI / model-identity marker. Fail closed.
        assert_no_identity_markers(observation_frame)

    phase = format_phase_id(state.current_phase)
    runtime = await runtime_repo.get(session, game_id)
    deadline_at = _as_aware(runtime.deadline_at) if runtime is not None else None

    return SeatObservationSnapshot(
        public_player_id=seat_row.public_player_id,
        phase=phase,
        identity_mode=identity_mode,
        observation_frame=observation_frame,
        deadline_frame=_deadline_frame(phase, deadline_at),
    )


def _sse_block(frame: dict[str, Any]) -> str:
    """Render one frame dict as an SSE ``data:`` block."""
    return f"data: {json.dumps(frame, separators=(',', ':'))}\n\n"


async def stream_snapshot(snapshot: SeatObservationSnapshot) -> AsyncGenerator[str, None]:
    """Yield the observation frame then the phase-deadline frame as SSE blocks.

    The snapshot is built (and authorized) BEFORE streaming begins so a
    wrong-seat / not-found error surfaces as a real HTTP status rather than a
    half-open stream. The deadline frame is transport-only — never persisted to
    the hash-chained log.
    """
    yield _sse_block(snapshot.observation_frame)
    yield _sse_block(snapshot.deadline_frame)


__all__ = [
    "DEADLINE_FRAME",
    "GAME_NOT_FOUND_DETAIL",
    "OBSERVATION_FRAME",
    "WRONG_SEAT_DETAIL",
    "SeatObservationSnapshot",
    "build_seat_observation_snapshot",
    "stream_snapshot",
]
