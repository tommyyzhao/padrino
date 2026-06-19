"""Durable, rehydratable human-game state (US-131).

A human-lane game can last minutes to hours, so a process restart must not lose
an in-progress game. This module rebuilds runner state for every in-progress
human game from two sources:

1. The hash-chained ``game_events`` log — the ONLY source of the deterministic
   core :class:`GameState`. It is replayed (and its hash chain verified) through
   the pure :func:`padrino.core.engine.replay.replay_event_log`, never read from
   the snapshot. If the snapshot ever disagreed with the event log, the event
   log wins (hard rule 4).
2. The ``human_game_runtime`` row — the *impure* live scaffolding only: the
   current phase, the phase wall-clock deadline, and a buffer snapshot of
   in-flight human submissions awaiting release.

This uses the existing async DB; there is no Redis (stack rule).

A game is eligible for rehydration when it is NOT terminal (status is not
``COMPLETED`` and the replayed state has no ``terminal_result``) and has at
least one HUMAN / AI_TAKEOVER seat (it is on the human lane). The benchmark
scheduler's AI-only games are untouched.

Impure runner module: it loads from the DB, but it imports no wall-clock or
random — the deadline is read straight from the persisted row.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.core.engine.event_log import EventLog, StoredEvent
from padrino.core.engine.events import EventAdapter
from padrino.core.engine.reducer import apply_event, initial_state
from padrino.core.engine.replay import replay_event_log
from padrino.core.engine.state import GameState
from padrino.core.enums import SeatKind
from padrino.db.models import GameEvent, GameSeat
from padrino.db.repositories import games as games_repo
from padrino.db.repositories import human_game_runtime as runtime_repo

_logger = structlog.get_logger("padrino.runner.human_durability")

STATUS_COMPLETED = "COMPLETED"

# A seat is "human-lane" when a human ever occupied it (a live human seat or a
# seat an AI silently took over). AI-only benchmark games have neither.
_HUMAN_LANE_SEAT_KINDS = frozenset({SeatKind.HUMAN.value, SeatKind.AI_TAKEOVER.value})


@dataclass(frozen=True, slots=True)
class RehydratedHumanGame:
    """Runner state rebuilt for one in-progress human game after a restart.

    ``state`` is the deterministic core state replayed from the event log (the
    authoritative source). ``phase`` / ``deadline_at`` / ``buffer_snapshot`` come
    from the persisted runtime row and let the impure shell resume the current
    phase exactly where it left off.
    """

    game_id: uuid.UUID
    state: GameState
    event_log: EventLog
    phase: str
    deadline_at: datetime | None
    buffer_snapshot: dict[str, object]


def _as_aware(value: datetime | None) -> datetime | None:
    """Coerce a (possibly tz-naive) stored timestamp back to UTC-aware.

    SQLite drops ``tzinfo`` from a ``DateTime(timezone=True)`` column, so a
    deadline persisted as UTC-aware loads back naive. Coercing it here keeps the
    resumed deadline comparable to a fresh wall-clock read in the runner.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _event_body(event: GameEvent) -> dict[str, object]:
    """Reconstruct the hashed event-body dict for one persisted row.

    Must match the shape produced by
    :func:`padrino.runner.game_runner._emit`:
    ``event_type, sequence, phase, visibility, actor_player_id, payload``.
    ``event_hash`` / ``prev_event_hash`` / ``created_at`` are excluded from the
    hash by :mod:`padrino.core.engine.hashing` and so are omitted here.
    """
    return {
        "event_type": event.event_type,
        "sequence": event.sequence,
        "phase": event.phase,
        "visibility": event.visibility,
        "actor_player_id": event.actor_player_id,
        "payload": event.payload,
    }


def replay_state_from_rows(rows: list[GameEvent]) -> tuple[GameState, EventLog]:
    """Rebuild ``(GameState, EventLog)`` from persisted event rows.

    The chain is verified: :func:`replay_event_log` re-seals each body and raises
    :class:`ReplayHashMismatchError` if a recomputed ``event_hash`` disagrees
    with the stored one. The state is then folded from the same bodies through
    the pure reducer.
    """
    stored = [
        StoredEvent(
            sequence=row.sequence,
            prev_event_hash=row.prev_event_hash,
            event_hash=row.event_hash,
            body=_event_body(row),
        )
        for row in rows
    ]
    event_log = replay_event_log(stored)
    state = initial_state()
    for prior in event_log.events:
        state = apply_event(state, EventAdapter.validate_python(prior.body))
    return state, event_log


async def _is_human_lane_game(session: AsyncSession, game_id: uuid.UUID) -> bool:
    stmt = select(GameSeat.seat_kind).where(GameSeat.game_id == game_id)
    kinds = (await session.execute(stmt)).scalars().all()
    return any(k in _HUMAN_LANE_SEAT_KINDS for k in kinds)


async def rehydrate_active_human_games(
    session_factory: async_sessionmaker[AsyncSession],
) -> list[RehydratedHumanGame]:
    """Rebuild runner state for every in-progress human game from the DB.

    For each ``human_game_runtime`` row whose game is human-lane and not yet
    terminal, replay the hash-chained event log to recover the core
    :class:`GameState` and resume the current phase from the runtime row. Returns
    the rehydrated games ordered by ``game_id`` for determinism.

    Terminal or non-human-lane games are skipped (a stale runtime row for a game
    that finished before the restart is ignored, not resumed).
    """
    rehydrated: list[RehydratedHumanGame] = []
    async with session_factory() as session:
        runtime_rows = await runtime_repo.list_all(session)
        for runtime in runtime_rows:
            game = await games_repo.get(session, runtime.game_id)
            if game is None or game.status == STATUS_COMPLETED:
                continue
            if not await _is_human_lane_game(session, runtime.game_id):
                continue

            event_stmt = (
                select(GameEvent)
                .where(GameEvent.game_id == runtime.game_id)
                .order_by(GameEvent.sequence)
            )
            rows = list((await session.execute(event_stmt)).scalars())
            if not rows:
                continue

            state, event_log = replay_state_from_rows(rows)
            if state.terminal_result is not None:
                # The event log shows the game already ended; never resume it.
                continue

            rehydrated.append(
                RehydratedHumanGame(
                    game_id=runtime.game_id,
                    state=state,
                    event_log=event_log,
                    phase=runtime.phase,
                    deadline_at=_as_aware(runtime.deadline_at),
                    buffer_snapshot=dict(runtime.buffer_snapshot),
                )
            )
            _logger.info(
                "human_game_rehydrated",
                game_id=str(runtime.game_id),
                phase=runtime.phase,
                sequence_head=event_log.events[-1].sequence,
            )

    rehydrated.sort(key=lambda r: r.game_id)
    return rehydrated


__all__ = [
    "RehydratedHumanGame",
    "rehydrate_active_human_games",
    "replay_state_from_rows",
]
