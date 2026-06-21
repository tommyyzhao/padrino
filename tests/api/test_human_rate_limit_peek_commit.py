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

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.human_actions import _enforce_action_rate_limits, _hash_key
from padrino.api.human_chat import _enforce_chat_rate_limits
from padrino.api.rate_limit_store import (
    DatabaseRateLimitStore,
    InMemoryRateLimitStore,
    RateLimitDecision,
)

RateLimitCall = Callable[[], Awaitable[None]]


class _BarrierDatabaseRateLimitStore:
    """Database-backed store that lines up same-key peeks before commits."""

    def __init__(
        self,
        *,
        inner: DatabaseRateLimitStore,
        barrier_key: str,
        participants: int,
    ) -> None:
        self._inner = inner
        self._barrier_key = barrier_key
        self._participants = participants
        self._peeked = 0
        self._lock = asyncio.Lock()
        self._all_peeked = asyncio.Event()

    async def peek_request(
        self,
        key_hash: str,
        *,
        now: float,
        limit_per_minute: int,
        window_seconds: float = 60.0,
    ) -> RateLimitDecision:
        decision = await self._inner.peek_request(
            key_hash,
            now=now,
            limit_per_minute=limit_per_minute,
            window_seconds=window_seconds,
        )
        if key_hash == self._barrier_key:
            async with self._lock:
                self._peeked += 1
                if self._peeked == self._participants:
                    self._all_peeked.set()
            await self._all_peeked.wait()
        return decision

    async def record_request(
        self,
        key_hash: str,
        *,
        now: float,
        limit_per_minute: int,
        window_seconds: float = 60.0,
    ) -> RateLimitDecision:
        return await self._inner.record_request(
            key_hash,
            now=now,
            limit_per_minute=limit_per_minute,
            window_seconds=window_seconds,
        )


def _principal_bucket_count(
    store: InMemoryRateLimitStore, *, namespace: str, principal_id: uuid.UUID, window: int
) -> int:
    key = _hash_key(f"{namespace}:user:{principal_id}")
    return store._counts.get((key, window), 0)


async def _admitted_or_429(call: RateLimitCall) -> bool:
    try:
        await call()
    except HTTPException as exc:
        assert exc.status_code == 429
        return False
    return True


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


async def test_action_database_commit_honors_atomic_record_rejection(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    inner = DatabaseRateLimitStore(session_factory=session_factory)
    principal_id = uuid.uuid4()
    game_id = uuid.uuid4()
    now = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)
    per_principal_limit = 2
    concurrent_requests = 6
    principal_key = _hash_key(f"human-action:user:{principal_id}")

    seeded = await inner.record_request(
        principal_key,
        now=now.timestamp(),
        limit_per_minute=per_principal_limit,
    )
    assert seeded.allowed

    store = _BarrierDatabaseRateLimitStore(
        inner=inner,
        barrier_key=principal_key,
        participants=concurrent_requests,
    )
    admitted = await asyncio.gather(
        *(
            _admitted_or_429(
                lambda: _enforce_action_rate_limits(
                    store,
                    principal_id=principal_id,
                    game_id=game_id,
                    phase="day-1",
                    now=now,
                    per_principal_limit=per_principal_limit,
                    per_game_phase_limit=100,
                )
            )
            for _ in range(concurrent_requests)
        )
    )

    assert sum(admitted) == 1


async def test_chat_database_commit_honors_atomic_record_rejection(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    inner = DatabaseRateLimitStore(session_factory=session_factory)
    principal_id = uuid.uuid4()
    game_id = uuid.uuid4()
    now = datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)
    per_principal_limit = 2
    concurrent_requests = 6
    principal_key = _hash_key(f"human-chat:user:{principal_id}")

    seeded = await inner.record_request(
        principal_key,
        now=now.timestamp(),
        limit_per_minute=per_principal_limit,
    )
    assert seeded.allowed

    store = _BarrierDatabaseRateLimitStore(
        inner=inner,
        barrier_key=principal_key,
        participants=concurrent_requests,
    )
    admitted = await asyncio.gather(
        *(
            _admitted_or_429(
                lambda: _enforce_chat_rate_limits(
                    store,
                    principal_id=principal_id,
                    game_id=game_id,
                    phase="day-1",
                    now=now,
                    per_principal_limit=per_principal_limit,
                    per_game_phase_limit=100,
                )
            )
            for _ in range(concurrent_requests)
        )
    )

    assert sum(admitted) == 1
