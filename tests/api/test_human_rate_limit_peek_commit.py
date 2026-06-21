"""Peek-then-commit multi-bucket rate-limit enforcement (US-203).

ROUND-2 REVIEW FINDING #9: the action and chat channels enforce two buckets
(per-principal, then per-game/phase). The old code called
``record_request`` per bucket, which INCREMENTS before returning a decision —
so when the per-game/phase bucket 429s, the per-principal bucket had already
been burned for a request that produced no accepted action. With
``per_game_phase < per_principal`` a seat could over-consume its per-principal
minute budget across its OTHER games/phases.

The fix is a non-incrementing :meth:`RateLimitStore.peek_request` plus an
all-or-nothing enforcement: peek every bucket, and only ``record_request`` all
of them once every peek admits. These tests assert that a per-game/phase
rejection does NOT consume the per-principal budget — for both channels.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException

from padrino.api.human_actions import _enforce_action_rate_limits, _hash_key
from padrino.api.human_chat import _enforce_chat_rate_limits
from padrino.api.rate_limit_store import InMemoryRateLimitStore


def _principal_bucket_count(
    store: InMemoryRateLimitStore, *, namespace: str, principal_id: uuid.UUID, window: int
) -> int:
    key = _hash_key(f"{namespace}:user:{principal_id}")
    return store._counts.get((key, window), 0)


async def test_action_phase_rejection_does_not_burn_principal_budget() -> None:
    store = InMemoryRateLimitStore()
    principal_id = uuid.uuid4()
    game_id = uuid.uuid4()
    now = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)
    window = int(now.timestamp() - (now.timestamp() % 60))

    # First request: both buckets admit, both committed.
    await _enforce_action_rate_limits(
        store,
        principal_id=principal_id,
        game_id=game_id,
        phase="day-1",
        now=now,
        per_principal_limit=5,
        per_game_phase_limit=1,
    )
    assert (
        _principal_bucket_count(
            store, namespace="human-action", principal_id=principal_id, window=window
        )
        == 1
    )

    # Second request in the SAME phase: the per-game/phase cap (1) is full, so
    # this 429s. The per-principal bucket must NOT be incremented.
    with pytest.raises(HTTPException) as exc_info:
        await _enforce_action_rate_limits(
            store,
            principal_id=principal_id,
            game_id=game_id,
            phase="day-1",
            now=now,
            per_principal_limit=5,
            per_game_phase_limit=1,
        )
    assert exc_info.value.status_code == 429

    # The principal bucket is still at 1: the rejected request did not burn it.
    assert (
        _principal_bucket_count(
            store, namespace="human-action", principal_id=principal_id, window=window
        )
        == 1
    )


async def test_chat_phase_rejection_does_not_burn_principal_budget() -> None:
    store = InMemoryRateLimitStore()
    principal_id = uuid.uuid4()
    game_id = uuid.uuid4()
    now = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)
    window = int(now.timestamp() - (now.timestamp() % 60))

    await _enforce_chat_rate_limits(
        store,
        principal_id=principal_id,
        game_id=game_id,
        phase="day-1",
        now=now,
        per_principal_limit=5,
        per_game_phase_limit=1,
    )
    assert (
        _principal_bucket_count(
            store, namespace="human-chat", principal_id=principal_id, window=window
        )
        == 1
    )

    with pytest.raises(HTTPException) as exc_info:
        await _enforce_chat_rate_limits(
            store,
            principal_id=principal_id,
            game_id=game_id,
            phase="day-1",
            now=now,
            per_principal_limit=5,
            per_game_phase_limit=1,
        )
    assert exc_info.value.status_code == 429

    assert (
        _principal_bucket_count(
            store, namespace="human-chat", principal_id=principal_id, window=window
        )
        == 1
    )


async def test_action_principal_rejection_does_not_double_count_phase() -> None:
    # Mirror case: when the per-principal cap is hit first, the per-game/phase
    # bucket must also not be committed (all-or-nothing in both directions).
    store = InMemoryRateLimitStore()
    principal_id = uuid.uuid4()
    game_id = uuid.uuid4()
    now = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)
    window = int(now.timestamp() - (now.timestamp() % 60))

    await _enforce_action_rate_limits(
        store,
        principal_id=principal_id,
        game_id=game_id,
        phase="day-1",
        now=now,
        per_principal_limit=1,
        per_game_phase_limit=5,
    )

    with pytest.raises(HTTPException):
        await _enforce_action_rate_limits(
            store,
            principal_id=principal_id,
            game_id=game_id,
            phase="day-1",
            now=now,
            per_principal_limit=1,
            per_game_phase_limit=5,
        )

    phase_key = _hash_key(f"human-action:game-phase-principal:{game_id}:day-1:{principal_id}")
    # The phase bucket was committed exactly once (the first, admitted request).
    assert store._counts.get((phase_key, window), 0) == 1
