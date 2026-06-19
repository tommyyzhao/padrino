"""Authenticated human chat channel into the buffered hold (US-135).

A human player submits a public/private chat message over an authenticated POST.
This impure shell:

* resolves the caller's seat in the game from ``occupant_principal_id`` (a human
  may only chat from the seat they occupy — a wrong-seat submission is rejected);
* validates the message respects the ruleset ``message_limits`` (over-limit is a
  422 at the request schema; an empty message is rejected here);
* validates the chat *channel* is legal for the seat in the current phase
  (PUBLIC chat in day discussion/vote; PRIVATE mafia chat in the night mafia
  channel), reusing the pure :func:`legal_actions_for` phase reading;
* parks the message in the buffer **hold** (``status='HELD'``) and runs it
  through the block-before-release moderation gate (US-140's
  :class:`RealtimeModerationHook`: deterministic first-pass + sanitizer +
  span-mask + an async guard model under a hard latency budget) BEFORE any
  release; a guard timeout/error falls back to the deterministic verdict so the
  game never halts;
* enforces a per-principal AND per-game/phase chat rate limit via RateLimitStore
  on each genuinely new message;
* on release routes the released/masked text to the out-of-band sidecar (US-123)
  — it is NEVER inlined in a hash-chained payload — and flips the hold to
  ``RELEASED``; a BLOCK is flipped to ``BLOCKED`` and never released/chained;
* dedupes retries with an idempotency key so a network retry never double-posts.

The chat firewall holds: nothing submitted here mutates game state — only a
structured ``Action`` (US-134) drives mechanics.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.api.human_chat_moderation import (
    ChatModerationHook,
    ChatVerdict,
    RealtimeModerationHook,
)
from padrino.api.rate_limit_store import RateLimitStore
from padrino.core.engine.state import Seat
from padrino.core.enums import Faction, PhaseKind
from padrino.core.observations import format_phase_id
from padrino.core.rulesets import get_ruleset
from padrino.db.models import GameSeat
from padrino.db.repositories import events as events_repo
from padrino.db.repositories import human_chat as sidecar_repo
from padrino.db.repositories import human_chat_submissions as holds_repo
from padrino.runner.human_durability import replay_state_from_rows

CHANNEL_PUBLIC = "PUBLIC"
CHANNEL_PRIVATE = "PRIVATE"

GAME_NOT_FOUND_DETAIL = "game_not_found"
WRONG_SEAT_DETAIL = "wrong_seat"
CHAT_NOT_ALLOWED_DETAIL = "chat_not_allowed"
EMPTY_MESSAGE_DETAIL = "empty_message"
OVER_LIMIT_DETAIL = "message_over_limit"
RATE_LIMITED_DETAIL = "chat_rate_limited"

# PUBLIC chat is legal during the day talk/vote phases; PRIVATE (mafia) chat in
# the night mafia channel. This mirrors the engine's chat-emitting phases.
_PUBLIC_CHAT_PHASES = frozenset({PhaseKind.DAY_DISCUSSION, PhaseKind.DAY_VOTE})
_PRIVATE_CHAT_PHASES = frozenset({PhaseKind.NIGHT_0_MAFIA_INTRO, PhaseKind.NIGHT_MAFIA_DISCUSSION})


@dataclass(frozen=True, slots=True)
class AcceptedChat:
    """The outcome of accepting (or replaying) one human chat submission."""

    public_player_id: str
    phase: str
    channel: str
    status: str
    idempotent_replay: bool


async def _resolve_seat(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    principal_id: uuid.UUID,
) -> GameSeat:
    """Return the seat the principal occupies in this game, or 403."""
    stmt = select(GameSeat).where(
        GameSeat.game_id == game_id,
        GameSeat.occupant_principal_id == principal_id,
    )
    seat = (await session.execute(stmt)).scalar_one_or_none()
    if seat is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=WRONG_SEAT_DETAIL)
    return seat


async def submit_chat(
    session: AsyncSession,
    *,
    game_id: uuid.UUID,
    principal_id: uuid.UUID,
    channel: str,
    text: str,
    idempotency_key: str,
    now: datetime,
    moderation: ChatModerationHook | None = None,
    rate_limit: RateLimitStore | None = None,
    per_principal_limit: int = 30,
    per_game_phase_limit: int = 120,
) -> AcceptedChat:
    """Buffer and (stub-)moderate a human's chat message for their seat.

    Raises :class:`fastapi.HTTPException` for an unknown game (404), a wrong-seat
    submission (403), an empty/over-limit message (422), or chat that is not
    legal for the seat's phase/channel (409). On success the message enters the
    buffer hold and is released only after the moderation hook passes; the raw
    text is routed to the sidecar on release. A retry with the same idempotency
    key returns the recorded message without inserting a duplicate.
    """
    seat_row = await _resolve_seat(session, game_id=game_id, principal_id=principal_id)

    cleaned = text.strip()
    if not cleaned:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=EMPTY_MESSAGE_DETAIL
        )

    rows = await events_repo.list_events(session, game_id)
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GAME_NOT_FOUND_DETAIL)

    state, _event_log = replay_state_from_rows(rows)
    if state.terminal_result is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=CHAT_NOT_ALLOWED_DETAIL)

    core_seat = state.seat_by_public_id(seat_row.public_player_id)
    if core_seat is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=WRONG_SEAT_DETAIL)
    if not core_seat.alive:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=CHAT_NOT_ALLOWED_DETAIL)

    ruleset = get_ruleset(state.ruleset_id)
    limit = (
        ruleset.PUBLIC_MESSAGE_MAX_CHARS
        if channel == CHANNEL_PUBLIC
        else ruleset.PRIVATE_MESSAGE_MAX_CHARS
    )
    if len(text) > limit:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=OVER_LIMIT_DETAIL
        )

    _enforce_channel_legal(channel, state.current_phase.kind, core_seat=core_seat)

    phase = format_phase_id(state.current_phase)
    existing = await holds_repo.get_by_idempotency_key(
        session,
        game_id=game_id,
        public_player_id=seat_row.public_player_id,
        phase=phase,
        idempotency_key=idempotency_key,
    )
    if existing is not None:
        return AcceptedChat(
            public_player_id=existing.public_player_id,
            phase=existing.phase,
            channel=existing.channel,
            status=existing.status,
            idempotent_replay=True,
        )

    # Per-user AND per-game/phase chat rate limits (US-140). Checked only on a
    # genuinely new message (after the idempotency check) so a retry never
    # consumes a slot. Both keys flow through the shared RateLimitStore.
    if rate_limit is not None:
        await _enforce_chat_rate_limits(
            rate_limit,
            principal_id=principal_id,
            game_id=game_id,
            phase=phase,
            now=now,
            per_principal_limit=per_principal_limit,
            per_game_phase_limit=per_game_phase_limit,
        )

    held = await holds_repo.record_held(
        session,
        game_id=game_id,
        public_player_id=seat_row.public_player_id,
        phase=phase,
        channel=channel,
        idempotency_key=idempotency_key,
        raw_text=text,
        created_at=now,
    )

    hook = moderation if moderation is not None else RealtimeModerationHook()
    decision = await hook.review(
        public_player_id=seat_row.public_player_id, channel=channel, text=text
    )

    if decision.verdict is ChatVerdict.BLOCK:
        await holds_repo.mark_blocked(session, submission=held)
        return AcceptedChat(
            public_player_id=held.public_player_id,
            phase=held.phase,
            channel=held.channel,
            status=held.status,
            idempotent_replay=False,
        )

    # ALLOW / SOFT_MASK release: route the raw + cleaned text to the sidecar
    # (US-123), NEVER inline in a hash-chained payload.
    sequence = await holds_repo.next_sidecar_sequence(session, game_id=game_id)
    await sidecar_repo.append_human_chat(
        session,
        game_id=game_id,
        sequence=sequence,
        public_player_id=seat_row.public_player_id,
        raw_text=text,
        cleaned_text=decision.cleaned_text,
    )
    await holds_repo.mark_released(
        session, submission=held, sidecar_sequence=sequence, released_at=now
    )
    return AcceptedChat(
        public_player_id=held.public_player_id,
        phase=held.phase,
        channel=held.channel,
        status=held.status,
        idempotent_replay=False,
    )


async def _enforce_chat_rate_limits(
    rate_limit: RateLimitStore,
    *,
    principal_id: uuid.UUID,
    game_id: uuid.UUID,
    phase: str,
    now: datetime,
    per_principal_limit: int,
    per_game_phase_limit: int,
) -> None:
    """Enforce the per-principal and per-game/phase chat ceilings, or 429.

    Both ceilings flow through the shared :class:`RateLimitStore`; the keys are
    namespaced (``human-chat:user`` vs ``human-chat:game-phase``) and hashed so a
    chat bucket never collides with an API-key or session bucket.
    """
    epoch = now.timestamp()
    principal_key = _hash_key(f"human-chat:user:{principal_id}")
    game_phase_key = _hash_key(f"human-chat:game-phase:{game_id}:{phase}")
    for key_hash, limit in (
        (principal_key, per_principal_limit),
        (game_phase_key, per_game_phase_limit),
    ):
        decision = await rate_limit.record_request(key_hash, now=epoch, limit_per_minute=limit)
        if not decision.allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=RATE_LIMITED_DETAIL,
                headers={"Retry-After": str(int(decision.retry_after_seconds))},
            )


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _enforce_channel_legal(channel: str, kind: PhaseKind, *, core_seat: Seat) -> None:
    """Reject chat that is not legal for the seat's channel in this phase."""
    if channel == CHANNEL_PUBLIC:
        if kind not in _PUBLIC_CHAT_PHASES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=CHAT_NOT_ALLOWED_DETAIL
            )
        return
    # PRIVATE: only a mafia seat in the night mafia channel.
    if kind not in _PRIVATE_CHAT_PHASES or core_seat.faction is not Faction.MAFIA:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=CHAT_NOT_ALLOWED_DETAIL)


__all__ = [
    "CHANNEL_PRIVATE",
    "CHANNEL_PUBLIC",
    "CHAT_NOT_ALLOWED_DETAIL",
    "EMPTY_MESSAGE_DETAIL",
    "GAME_NOT_FOUND_DETAIL",
    "OVER_LIMIT_DETAIL",
    "RATE_LIMITED_DETAIL",
    "WRONG_SEAT_DETAIL",
    "AcceptedChat",
    "submit_chat",
]
