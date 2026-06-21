"""Authenticated human action channel (US-134).

A human player submits a structured :class:`padrino.core.engine.actions.Action`
(``type`` + optional ``target``) for their own seat over an authenticated POST.
This impure shell:

* resolves the caller's seat in the game from ``occupant_principal_id`` (a human
  may only act for the seat they occupy — a wrong-seat submission is rejected);
* resolves the deterministic core state from the durable human runtime cache,
  reading only events committed after the cached head when possible, then
  validates the action against
  :func:`padrino.core.engine.legal_actions.legal_actions_for` for the seat in
  the *current* phase (illegal type / illegal target / out-of-phase are rejected);
* enforces the chat firewall — ONLY the structured ``Action`` is accepted here;
  chat is a separate channel (US-135). The action drives state, nothing else.
* dedupes retries with an idempotency key so a network retry never double-votes.
* enforces action-channel rate limits distinct from the shared session bucket —
  but ONLY after legality passes (US-191), so a now-illegal action (e.g. a vote
  on a target eliminated between observe and submit) never burns the seat's
  rate budget and 429-locks it out of its one legal move for the phase.

All validation reads the pure core (``legal_actions_for``); no mechanics live
here. The store is :class:`padrino.db.models.HumanActionSubmission`.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.api.human_seat_auth import resolve_human_game_seat
from padrino.api.rate_limit_store import InMemoryRateLimitStore, RateLimitStore
from padrino.core.engine.actions import Action
from padrino.core.engine.legal_actions import legal_actions_for
from padrino.core.enums import ActionType
from padrino.core.observations import format_phase_id
from padrino.db.repositories import human_action_submissions as submissions_repo
from padrino.db.repositories import human_seat_presence as presence_repo
from padrino.runner.human_state_cache import resolve_current_human_state

# Targeted actions require a legal ``target``; the rest must carry no target.
_TARGETED_ACTION_TYPES = frozenset(
    {ActionType.VOTE, ActionType.MAFIA_KILL, ActionType.PROTECT, ActionType.INVESTIGATE}
)

GAME_NOT_FOUND_DETAIL = "game_not_found"
WRONG_SEAT_DETAIL = "wrong_seat"
ILLEGAL_ACTION_DETAIL = "illegal_action"
OUT_OF_PHASE_DETAIL = "out_of_phase"
RATE_LIMITED_DETAIL = "action_rate_limited"
_DEFAULT_ACTION_RATE_LIMIT_STORE = InMemoryRateLimitStore()


@dataclass(frozen=True, slots=True)
class AcceptedAction:
    """The outcome of accepting (or replaying) one human action submission."""

    public_player_id: str
    phase: str
    action_type: str
    target: str | None
    idempotent_replay: bool


async def submit_action(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    principal_id: uuid.UUID,
    action: Action,
    idempotency_key: str,
    now: datetime,
    rate_limit: RateLimitStore | None = None,
    per_principal_limit: int = 60,
    per_game_phase_limit: int = 30,
) -> AcceptedAction:
    """Validate and buffer a human's structured action for their seat.

    Raises :class:`fastapi.HTTPException` for an unknown game (404), a wrong-seat
    submission (403), or an illegal / out-of-phase action (409). On success the
    action is stored; a retry with the same idempotency key returns the recorded
    action without inserting a duplicate (no double-vote).
    """
    seat_row = await resolve_human_game_seat(
        session,
        game_id=game_id,
        principal_id=principal_id,
        wrong_seat_detail=WRONG_SEAT_DETAIL,
    )

    # Cheap existence / terminal checks run BEFORE the presence heartbeat: do not
    # heartbeat (nor consume any budget) for an unknown or already-finished game.
    resolved = await resolve_current_human_state(session, game_id)
    if resolved is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GAME_NOT_FOUND_DETAIL)

    state = resolved.state
    if state.terminal_result is not None:
        # A finished game accepts no further actions.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=OUT_OF_PHASE_DETAIL)

    core_seat = state.seat_by_public_id(seat_row.public_player_id)
    if core_seat is None:
        # The seat exists in the DB but not in the replayed state — treat as wrong seat.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=WRONG_SEAT_DETAIL)

    await presence_repo.record_heartbeat(
        session,
        game_id=game_id,
        public_player_id=seat_row.public_player_id,
        seen_at=now,
    )

    phase = format_phase_id(state.current_phase)
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

    # Validate legality BEFORE consuming a rate-limit slot (US-191 / FINDING 3).
    # An illegal / out-of-phase action — including the normal live-game race where
    # a vote target was eliminated between observe and submit — returns 409 WITHOUT
    # burning the seat's per-principal / per-game-phase budget, so a seat is never
    # 429-locked out of its ONE legal move for the phase by prior rejected tries.
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

    # The action is legal: only now consume a rate-limit slot.
    await _enforce_action_rate_limits(
        rate_limit if rate_limit is not None else _DEFAULT_ACTION_RATE_LIMIT_STORE,
        principal_id=principal_id,
        game_id=game_id,
        phase=phase,
        now=now,
        per_principal_limit=per_principal_limit,
        per_game_phase_limit=per_game_phase_limit,
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


async def _enforce_action_rate_limits(
    rate_limit: RateLimitStore,
    *,
    principal_id: uuid.UUID,
    game_id: uuid.UUID,
    phase: str,
    now: datetime,
    per_principal_limit: int,
    per_game_phase_limit: int,
) -> None:
    epoch = now.timestamp()
    principal_key = _hash_key(f"human-action:user:{principal_id}")
    game_phase_key = _hash_key(
        f"human-action:game-phase-principal:{game_id}:{phase}:{principal_id}"
    )
    buckets = (
        (principal_key, per_principal_limit),
        (game_phase_key, per_game_phase_limit),
    )
    # Peek-then-commit: verify every bucket would admit the request BEFORE
    # incrementing any. A later bucket's rejection must not burn an earlier
    # bucket's per-minute budget across the seat's other games/phases.
    for key_hash, limit in buckets:
        decision = await rate_limit.peek_request(key_hash, now=epoch, limit_per_minute=limit)
        if not decision.allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=RATE_LIMITED_DETAIL,
                headers={"Retry-After": str(int(decision.retry_after_seconds))},
            )
    for key_hash, limit in buckets:
        await rate_limit.record_request(key_hash, now=epoch, limit_per_minute=limit)


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


__all__ = [
    "GAME_NOT_FOUND_DETAIL",
    "ILLEGAL_ACTION_DETAIL",
    "OUT_OF_PHASE_DETAIL",
    "RATE_LIMITED_DETAIL",
    "WRONG_SEAT_DETAIL",
    "AcceptedAction",
    "submit_action",
]
