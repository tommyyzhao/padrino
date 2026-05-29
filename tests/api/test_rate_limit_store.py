"""Tests for the shared rate-limit store backings (US-074).

The wave-2 in-process counter has its own coverage in ``test_auth.py``;
this module focuses on the new :class:`RateLimitStore` Protocol, the
:class:`InMemoryRateLimitStore` ceiling and eviction behavior, and the
:class:`DatabaseRateLimitStore` cross-worker contention scenario that
US-074 specifically calls out.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import RateLimiter, generate_raw_key
from padrino.api.rate_limit_store import (
    DatabaseRateLimitStore,
    InMemoryRateLimitStore,
    RateLimitStore,
)
from padrino.db.models import RateLimitBucket
from padrino.db.repositories import api_keys as api_keys_repo


@pytest.fixture(autouse=True)
def _stub_provider_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-cerebras-key")


# --- Protocol ----------------------------------------------------------------


async def test_in_memory_store_satisfies_protocol() -> None:
    store: RateLimitStore = InMemoryRateLimitStore()
    assert isinstance(store, RateLimitStore)


async def test_database_store_satisfies_protocol(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store: RateLimitStore = DatabaseRateLimitStore(session_factory=session_factory)
    assert isinstance(store, RateLimitStore)


# --- In-memory ceiling + eviction --------------------------------------------


async def test_in_memory_store_enforces_ceiling_within_window() -> None:
    store = InMemoryRateLimitStore()
    key_hash = "h" * 64
    now = 1_000_000.0
    d1 = await store.record_request(key_hash, now=now, limit_per_minute=2)
    d2 = await store.record_request(key_hash, now=now + 1, limit_per_minute=2)
    d3 = await store.record_request(key_hash, now=now + 2, limit_per_minute=2)
    assert d1.allowed and d2.allowed
    assert not d3.allowed
    assert d3.retry_after_seconds > 0


async def test_in_memory_store_drains_after_window_advances() -> None:
    store = InMemoryRateLimitStore()
    key_hash = "h" * 64
    now = 1_000_000.0
    for _ in range(2):
        decision = await store.record_request(key_hash, now=now, limit_per_minute=2)
        assert decision.allowed
    # Advance to the next 60s window.
    decision = await store.record_request(key_hash, now=now + 61.0, limit_per_minute=2)
    assert decision.allowed


async def test_in_memory_store_evicts_stale_buckets() -> None:
    store = InMemoryRateLimitStore()
    now = 1_000_000.0
    await store.record_request("aaa", now=now, limit_per_minute=10)
    await store.record_request("bbb", now=now, limit_per_minute=10)
    # Two windows later, stale buckets should be gone.
    await store.record_request("ccc", now=now + 180.0, limit_per_minute=10)
    assert all(window >= int(now + 180.0 - (now + 180.0) % 60) - 60 for _, window in store._counts)
    # The new bucket is the only one for ccc; the old aaa/bbb buckets evicted.
    assert ("aaa", int(now - (now % 60))) not in store._counts
    assert ("bbb", int(now - (now % 60))) not in store._counts


# --- Database store: cross-session contention --------------------------------


async def test_database_store_two_simulated_workers_share_one_bucket(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two stores (one per worker) hitting the same key land in one bucket.

    This is the exact scenario US-074 documents: each replica builds its
    own ``DatabaseRateLimitStore`` over the same Postgres URL; the shared
    counter has to atomically increment so the worker count doesn't
    multiply the effective ceiling.
    """
    worker_a = DatabaseRateLimitStore(session_factory=session_factory)
    worker_b = DatabaseRateLimitStore(session_factory=session_factory)
    key_hash = "deadbeef" * 8
    now = 1_000_000.0
    decisions = await asyncio.gather(
        worker_a.record_request(key_hash, now=now, limit_per_minute=2),
        worker_b.record_request(key_hash, now=now + 0.001, limit_per_minute=2),
    )
    assert all(d.allowed for d in decisions)
    # Third hit (worker_a again) should exceed the shared ceiling.
    third = await worker_a.record_request(key_hash, now=now + 0.002, limit_per_minute=2)
    assert not third.allowed

    async with session_factory() as session:
        rows = list((await session.execute(select(RateLimitBucket))).scalars())
    assert len(rows) == 1
    assert rows[0].count == 3


async def test_database_store_evicts_stale_windows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A write at window N+2 should delete rows from window N."""
    store = DatabaseRateLimitStore(session_factory=session_factory)
    key_hash = "abc" * 8
    await store.record_request(key_hash, now=1_000_000.0, limit_per_minute=5)
    # Confirm bucket exists.
    async with session_factory() as session:
        rows_before = list((await session.execute(select(RateLimitBucket))).scalars())
    assert len(rows_before) == 1

    await store.record_request(key_hash, now=1_000_000.0 + 180.0, limit_per_minute=5)
    async with session_factory() as session:
        rows_after = list((await session.execute(select(RateLimitBucket))).scalars())
    assert len(rows_after) == 1
    assert rows_after[0].window_start > rows_before[0].window_start


async def test_database_store_separate_keys_track_independently(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = DatabaseRateLimitStore(session_factory=session_factory)
    now = 1_000_000.0
    d_a = await store.record_request("aaaa", now=now, limit_per_minute=1)
    d_b = await store.record_request("bbbb", now=now, limit_per_minute=1)
    assert d_a.allowed and d_b.allowed
    # Each key has used its single allowance.
    d_a2 = await store.record_request("aaaa", now=now, limit_per_minute=1)
    assert not d_a2.allowed


# --- Disabled key short-circuits before rate limit ---------------------------


async def test_disabled_key_returns_401_not_429_mid_window(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A key disabled while inside its rate window should 401 immediately.

    The auth path checks ``disabled_at`` BEFORE consulting the rate
    limiter, so an admin disable mid-burst flips the request to 401
    rather than letting a rate-limited caller keep hammering the 429
    path. This is a guard against the privilege-revocation race.
    """
    app = create_app(
        session_factory=session_factory,
        auth_required=True,
        rate_limiter=RateLimiter(),
    )
    transport = ASGITransport(app=app)
    raw = generate_raw_key()
    async with session_factory() as session, session.begin():
        record = await api_keys_repo.create(
            session,
            raw_key=raw,
            scopes=["spectator"],
            label="will-disable",
        )
        key_id = record.id

    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        # Use up one request slot.
        ok = await ac.get(
            "/model-providers",
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert ok.status_code == 200

        # Disable the key mid-window.
        async with session_factory() as session, session.begin():
            await api_keys_repo.disable(session, key_id)

        denied = await ac.get(
            "/model-providers",
            headers={"Authorization": f"Bearer {raw}"},
        )
    assert denied.status_code == 401
    assert denied.json()["detail"] == "api_key_disabled"


# --- Auto-selection in create_app -------------------------------------------


def test_create_app_uses_in_memory_store_for_sqlite(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = create_app(session_factory=session_factory, auth_required=True)
    limiter = app.state.rate_limiter
    assert isinstance(limiter.store, InMemoryRateLimitStore)


def test_create_app_uses_in_memory_store_when_workers_is_one(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PADRINO_DB_URL", "postgresql+asyncpg://u:p@host/db")
    monkeypatch.setenv("PADRINO_API_WORKERS", "1")
    from padrino import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    try:
        app = create_app(session_factory=session_factory, auth_required=True)
        limiter = app.state.rate_limiter
        assert isinstance(limiter.store, InMemoryRateLimitStore)
    finally:
        settings_mod.get_settings.cache_clear()


def test_create_app_selects_database_store_for_multi_worker_postgres(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PADRINO_DB_URL", "postgresql+asyncpg://u:p@host/db")
    monkeypatch.setenv("PADRINO_API_WORKERS", "4")
    from padrino import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    try:
        app = create_app(session_factory=session_factory, auth_required=True)
        limiter = app.state.rate_limiter
        assert isinstance(limiter.store, DatabaseRateLimitStore)
    finally:
        settings_mod.get_settings.cache_clear()


def test_create_app_explicit_store_overrides_auto_selection(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    explicit = InMemoryRateLimitStore()
    app = create_app(
        session_factory=session_factory,
        auth_required=True,
        rate_limit_store=explicit,
    )
    assert app.state.rate_limiter.store is explicit


# --- 429 path with Retry-After header (sanity) -------------------------------


async def test_auth_path_returns_429_with_retry_after(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClock:
        def __init__(self) -> None:
            self.now = 1_000_000.0

        def __call__(self) -> float:
            return self.now

    clock = FakeClock()
    limiter = RateLimiter(clock=clock)
    app = create_app(
        session_factory=session_factory,
        auth_required=True,
        rate_limiter=limiter,
    )
    monkeypatch.setattr(
        "padrino.api.auth._limit_for_scopes",
        lambda scopes, settings: 1,
    )
    transport = ASGITransport(app=app)
    raw = generate_raw_key()
    async with session_factory() as session, session.begin():
        await api_keys_repo.create(
            session,
            raw_key=raw,
            scopes=["spectator"],
            label="rl-test",
        )

    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        headers = {"Authorization": f"Bearer {raw}"}
        first = await ac.get("/model-providers", headers=headers)
        assert first.status_code == 200
        second = await ac.get("/model-providers", headers=headers)
    assert second.status_code == 429
    assert "Retry-After" in second.headers
    assert int(second.headers["Retry-After"]) >= 1


# Make uuid and other unused names show up so linters don't strip imports.
assert uuid is not None
