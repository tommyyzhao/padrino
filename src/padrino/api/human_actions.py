"""Authenticated human action channel (US-134).

A human player submits a structured :class:`padrino.core.engine.actions.Action`
(``type`` + optional ``target``) for their own seat over an authenticated POST.
This impure shell:

* resolves the caller's seat in the game from ``occupant_principal_id`` (a human
  may only act for the seat they occupy — a wrong-seat submission is rejected);
* replays the hash-chained event log to recover the deterministic core
  :class:`padrino.core.engine.state.GameState`, then validates the action against
  :func:`padrino.core.engine.legal_actions.legal_actions_for` for the seat in the
  *current* phase (illegal type / illegal target / out-of-phase are rejected);
* enforces the chat firewall — ONLY the structured ``Action`` is accepted here;
  chat is a separate channel (US-135). The action drives state, nothing else.
* dedupes retries with an idempotency key so a network retry never double-votes.

All validation reads the pure core (``legal_actions_for``); no mechanics live
here. The store is :class:`padrino.db.models.HumanActionSubmission`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.engine.actions import Action
from padrino.core.engine.legal_actions import legal_actions_for
from padrino.core.enums import ActionType
from padrino.core.observations import format_phase_id
from padrino.db.models import GameSeat
from padrino.db.repositories import events as events_repo
from padrino.db.repositories import human_action_submissions as submissions_repo
from padrino.db.repositories import human_seat_presence as presence_repo
from padrino.runner.human_durability import replay_state_from_rows

# Targeted actions require a legal ``target``; the rest must carry no target.
_TARGETED_ACTION_TYPES = frozenset(
    {ActionType.VOTE, ActionType.MAFIA_KILL, ActionType.PROTECT, ActionType.INVESTIGATE}
)

GAME_NOT_FOUND_DETAIL = "game_not_found"
WRONG_SEAT_DETAIL = "wrong_seat"
ILLEGAL_ACTION_DETAIL = "illegal_action"
OUT_OF_PHASE_DETAIL = "out_of_phase"


@dataclass(frozen=True, slots=True)
class AcceptedAction:
    """The outcome of accepting (or replaying) one human action submission."""

    public_player_id: str
    phase: str
    action_type: str
    target: str | None
    idempotent_replay: bool


async def _resolve_seat(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    principal_id: uuid.UUID,
) -> GameSeat:
    """Return the seat the principal occupies in this game, or 403.

    A human may act ONLY for the seat they occupy. A submission for a game the
    principal has no seat in (or any other seat) is a wrong-seat rejection.
    """
    stmt = select(GameSeat).where(
        GameSeat.game_id == game_id,
        GameSeat.occupant_principal_id == principal_id,
    )
    seat = (await session.execute(stmt)).scalar_one_or_none()
    if seat is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=WRONG_SEAT_DETAIL)
    return seat


async def submit_action(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    principal_id: uuid.UUID,
    action: Action,
    idempotency_key: str,
    now: datetime,
) -> AcceptedAction:
    """Validate and buffer a human's structured action for their seat.

    Raises :class:`fastapi.HTTPException` for an unknown game (404), a wrong-seat
    submission (403), or an illegal / out-of-phase action (409). On success the
    action is stored; a retry with the same idempotency key returns the recorded
    action without inserting a duplicate (no double-vote).
    """
    seat_row = await _resolve_seat(session, game_id=game_id, principal_id=principal_id)
    await presence_repo.record_heartbeat(
        session,
        game_id=game_id,
        public_player_id=seat_row.public_player_id,
        seen_at=now,
    )

    rows = await events_repo.list_events(session, game_id)
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GAME_NOT_FOUND_DETAIL)

    state, _event_log = replay_state_from_rows(rows)
    if state.terminal_result is not None:
        # A finished game accepts no further actions.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=OUT_OF_PHASE_DETAIL)

    core_seat = state.seat_by_public_id(seat_row.public_player_id)
    if core_seat is None:
        # The seat exists in the DB but not in the replayed state — treat as wrong seat.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=WRONG_SEAT_DETAIL)

    phase = format_phase_id(state.current_phase)
    legal = legal_actions_for(state, core_seat)

    if action.type not in legal.allowed_action_types:
        # No legal action types means the seat cannot act in this phase at all.
        detail = OUT_OF_PHASE_DETAIL if not legal.allowed_action_types else ILLEGAL_ACTION_DETAIL
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)

    if action.type in _TARGETED_ACTION_TYPES:
        if action.target is None or action.target not in legal.legal_targets:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=ILLEGAL_ACTION_DETAIL)
    elif action.target is not None:
        # NOOP / ABSTAIN must not carry a target.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=ILLEGAL_ACTION_DETAIL)

    existing = await submissions_repo.get_by_idempotency_key(
        session,
        game_id=game_id,
        public_player_id=seat_row.public_player_id,
        phase=phase,
        idempotency_key=idempotency_key,
    )
    if existing is not None:
        return AcceptedAction(
            public_player_id=existing.public_player_id,
            phase=existing.phase,
            action_type=existing.action_type,
            target=existing.target,
            idempotent_replay=True,
        )

    record = await submissions_repo.record(
        session,
        game_id=game_id,
        public_player_id=seat_row.public_player_id,
        phase=phase,
        idempotency_key=idempotency_key,
        action_type=action.type.value,
        target=action.target,
        created_at=now,
    )
    return AcceptedAction(
        public_player_id=record.public_player_id,
        phase=record.phase,
        action_type=record.action_type,
        target=record.target,
        idempotent_replay=False,
    )


__all__ = [
    "GAME_NOT_FOUND_DETAIL",
    "ILLEGAL_ACTION_DETAIL",
    "OUT_OF_PHASE_DETAIL",
    "WRONG_SEAT_DETAIL",
    "AcceptedAction",
    "submit_action",
]
