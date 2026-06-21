"""Shared-state rate-limit storage backends (US-074).

The wave-2 :class:`padrino.api.auth.RateLimiter` kept a deque of timestamps
per key inside a single process. With multiple uvicorn workers / replicas
each replica enforces its own ceiling, so the effective limit multiplies
by worker count — the opposite of what an operator setting "30 req/min"
expects.

This module factors the counter behind a :class:`RateLimitStore` Protocol
and ships two implementations:

* :class:`InMemoryRateLimitStore` — fixed-window counter held in a process
  dict. Default for tests and single-process dev deployments.
* :class:`DatabaseRateLimitStore` — fixed-window counter persisted in the
  ``rate_limit_buckets`` table (migration 0012). Multiple workers reading
  / writing the same row share a single ceiling. Eviction of stale windows
  runs on every write so the table stays bounded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.db.models import RateLimitBucket


@dataclass(frozen=True)
class RateLimitDecision:
    """The outcome of one :meth:`RateLimitStore.record_request` call."""

    allowed: bool
    retry_after_seconds: float


@runtime_checkable
class RateLimitStore(Protocol):
    """Per-key fixed-window rate-limit counter.

    Each call records one request against the bucket identified by
    ``(key_hash, window_start)`` where ``window_start = int(now //
    window_seconds) * window_seconds``. The bucket counts requests for
    the current window; when ``count`` would exceed ``limit_per_minute``
    the call returns ``allowed=False`` with ``retry_after_seconds`` set
    to the time remaining in the current window.
    """

    async def record_request(
        self,
        key_hash: str,
        *,
        now: float,
        limit_per_minute: int,
        window_seconds: float = 60.0,
    ) -> RateLimitDecision: ...

    async def peek_request(
        self,
        key_hash: str,
        *,
        now: float,
        limit_per_minute: int,
        window_seconds: float = 60.0,
    ) -> RateLimitDecision:
        """Return whether one more request WOULD be admitted, without recording.

        This is the read-only half of a peek-then-commit sequence: a caller
        enforcing several buckets at once peeks every bucket first and only
        :meth:`record_request`-s them all once every peek admits, so a later
        bucket's rejection never burns an earlier bucket's increment.
        """
        ...


def _window_start(now: float, window_seconds: float) -> int:
    return int(now - (now % window_seconds))


@dataclass
class InMemoryRateLimitStore:
    """Process-local fixed-window counter.

    Suitable for single-process deployments and unit tests. Two processes
    sharing this store via shared memory is NOT supported — pick
    :class:`DatabaseRateLimitStore` for that.
    """

    _counts: dict[tuple[str, int], int] = field(default_factory=dict)

    async def record_request(
        self,
        key_hash: str,
        *,
        now: float,
        limit_per_minute: int,
        window_seconds: float = 60.0,
    ) -> RateLimitDecision:
        window_start = _window_start(now, window_seconds)
        cutoff = window_start - int(window_seconds)
        for bucket_key in list(self._counts):
            if bucket_key[1] < cutoff:
                del self._counts[bucket_key]
        current = self._counts.get((key_hash, window_start), 0)
        if current >= limit_per_minute:
            retry = max(1.0, (window_start + window_seconds) - now)
            return RateLimitDecision(allowed=False, retry_after_seconds=retry)
        self._counts[(key_hash, window_start)] = current + 1
        return RateLimitDecision(allowed=True, retry_after_seconds=0.0)

    async def peek_request(
        self,
        key_hash: str,
        *,
        now: float,
        limit_per_minute: int,
        window_seconds: float = 60.0,
    ) -> RateLimitDecision:
        window_start = _window_start(now, window_seconds)
        current = self._counts.get((key_hash, window_start), 0)
        if current >= limit_per_minute:
            retry = max(1.0, (window_start + window_seconds) - now)
            return RateLimitDecision(allowed=False, retry_after_seconds=retry)
        return RateLimitDecision(allowed=True, retry_after_seconds=0.0)

    def reset(self) -> None:
        self._counts.clear()


@dataclass
class DatabaseRateLimitStore:
    """Shared fixed-window counter backed by ``rate_limit_buckets``.

    Uses ``INSERT ... ON CONFLICT DO UPDATE`` so two workers writing to the
    same bucket atomically increment a single ``count`` column. Stale
    windows are deleted on every write to keep the table from growing
    unbounded; the eviction cutoff is one window behind the current
    window so an in-flight request that lapped the window boundary is
    never dropped.

    Both SQLite and Postgres are supported — the dialect is detected from
    the session bind so the test suite can exercise the same code path
    that the production Postgres deployment runs.
    """

    session_factory: async_sessionmaker[AsyncSession]

    async def record_request(
        self,
        key_hash: str,
        *,
        now: float,
        limit_per_minute: int,
        window_seconds: float = 60.0,
    ) -> RateLimitDecision:
        window_start = _window_start(now, window_seconds)
        cutoff = window_start - int(window_seconds)
        async with self.session_factory() as session, session.begin():
            await session.execute(
                delete(RateLimitBucket).where(RateLimitBucket.window_start < cutoff)
            )
            new_count = await _upsert_count(
                session,
                key_hash=key_hash,
                window_start=window_start,
            )
        if new_count > limit_per_minute:
            retry = max(1.0, (window_start + window_seconds) - now)
            return RateLimitDecision(allowed=False, retry_after_seconds=retry)
        return RateLimitDecision(allowed=True, retry_after_seconds=0.0)

    async def peek_request(
        self,
        key_hash: str,
        *,
        now: float,
        limit_per_minute: int,
        window_seconds: float = 60.0,
    ) -> RateLimitDecision:
        window_start = _window_start(now, window_seconds)
        async with self.session_factory() as session:
            current = await session.scalar(
                select(RateLimitBucket.count).where(
                    RateLimitBucket.key_hash == key_hash,
                    RateLimitBucket.window_start == window_start,
                )
            )
        if current is not None and current >= limit_per_minute:
            retry = max(1.0, (window_start + window_seconds) - now)
            return RateLimitDecision(allowed=False, retry_after_seconds=retry)
        return RateLimitDecision(allowed=True, retry_after_seconds=0.0)


async def _upsert_count(
    session: AsyncSession,
    *,
    key_hash: str,
    window_start: int,
) -> int:
    """Atomically increment the bucket count, returning the new value.

    Uses the dialect-specific ``INSERT ... ON CONFLICT DO UPDATE`` so two
    concurrent workers contending on the same primary key produce two
    distinct ``count`` values (1, then 2) without lost updates.
    """
    bind = session.get_bind()
    dialect_name = bind.dialect.name
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        pg_stmt = (
            pg_insert(RateLimitBucket)
            .values(key_hash=key_hash, window_start=window_start, count=1)
            .on_conflict_do_update(
                index_elements=["key_hash", "window_start"],
                set_={"count": RateLimitBucket.count + 1},
            )
            .returning(RateLimitBucket.count)
        )
        result = await session.execute(pg_stmt)
        return int(result.scalar_one())
    if dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        sqlite_stmt = (
            sqlite_insert(RateLimitBucket)
            .values(key_hash=key_hash, window_start=window_start, count=1)
            .on_conflict_do_update(
                index_elements=["key_hash", "window_start"],
                set_={"count": RateLimitBucket.count + 1},
            )
            .returning(RateLimitBucket.count)
        )
        result = await session.execute(sqlite_stmt)
        return int(result.scalar_one())
    raise RuntimeError(f"unsupported dialect for DatabaseRateLimitStore: {dialect_name!r}")


__all__ = [
    "DatabaseRateLimitStore",
    "InMemoryRateLimitStore",
    "RateLimitDecision",
    "RateLimitStore",
]
