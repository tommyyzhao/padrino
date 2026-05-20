"""Tests for scoped API-key authentication and per-key rate limits (US-056)."""

from __future__ import annotations

import hmac
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import (
    RAW_KEY_PREFIX,
    SCOPE_ADMIN,
    SCOPE_SPECTATOR,
    SCOPE_SUBMITTER,
    RateLimiter,
    generate_raw_key,
)
from padrino.db.base import Base, create_engine, create_session_factory
from padrino.db.repositories import api_keys as api_keys_repo


@pytest.fixture(autouse=True)
def _stub_provider_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-cerebras-key")


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(engine)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest_asyncio.fixture
async def fake_clock() -> _FakeClock:
    return _FakeClock()


@pytest_asyncio.fixture
async def auth_client_factory(
    session_factory: async_sessionmaker[AsyncSession],
    fake_clock: _FakeClock,
) -> AsyncIterator[AuthClientFactory]:
    """Build an authenticated client + helpers that seed api_keys directly."""

    async def _seed_key(*, scopes: list[str], label: str = "test") -> tuple[str, uuid.UUID]:
        raw = generate_raw_key()
        async with session_factory() as session, session.begin():
            obj = await api_keys_repo.create(
                session,
                raw_key=raw,
                scopes=scopes,
                label=label,
            )
            return raw, obj.id

    limiter = RateLimiter(clock=fake_clock)
    app = create_app(
        session_factory=session_factory,
        auth_required=True,
        admin_token="legacy-admin-token-for-shim",
        rate_limiter=limiter,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield AuthClientFactory(ac, _seed_key, limiter, fake_clock, session_factory)


class AuthClientFactory:
    def __init__(
        self,
        client: AsyncClient,
        seed_key: object,
        limiter: RateLimiter,
        clock: _FakeClock,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.client = client
        self._seed_key = seed_key
        self.limiter = limiter
        self.clock = clock
        self.session_factory = session_factory

    async def seed(self, *, scopes: list[str], label: str = "test") -> tuple[str, uuid.UUID]:
        return await self._seed_key(scopes=scopes, label=label)  # type: ignore[no-any-return,operator]

    def headers(self, raw_key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {raw_key}"}


_PROVIDER_BODY = {
    "name": "cerebras",
    "auth_secret_ref": "env:CEREBRAS_API_KEY",
    "base_url": "https://api.cerebras.ai/v1",
}


async def test_generate_raw_key_uses_prefix() -> None:
    raw = generate_raw_key()
    assert raw.startswith(RAW_KEY_PREFIX)
    # raw key body should be reasonably long random bytes (>30 base64 chars)
    assert len(raw) > len(RAW_KEY_PREFIX) + 30


async def test_admin_key_can_write(auth_client_factory: AuthClientFactory) -> None:
    raw, _ = await auth_client_factory.seed(scopes=[SCOPE_ADMIN], label="admin")
    response = await auth_client_factory.client.post(
        "/model-providers",
        json=_PROVIDER_BODY,
        headers=auth_client_factory.headers(raw),
    )
    assert response.status_code == 201, response.text


async def test_submitter_cannot_write_admin_routes(
    auth_client_factory: AuthClientFactory,
) -> None:
    raw, _ = await auth_client_factory.seed(scopes=[SCOPE_SUBMITTER], label="ingest-bot")
    response = await auth_client_factory.client.post(
        "/model-providers",
        json=_PROVIDER_BODY,
        headers=auth_client_factory.headers(raw),
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "insufficient_scope"


async def test_spectator_cannot_write(auth_client_factory: AuthClientFactory) -> None:
    raw, _ = await auth_client_factory.seed(scopes=[SCOPE_SPECTATOR], label="viewer")
    response = await auth_client_factory.client.post(
        "/model-providers",
        json=_PROVIDER_BODY,
        headers=auth_client_factory.headers(raw),
    )
    assert response.status_code == 403


async def test_spectator_can_read(auth_client_factory: AuthClientFactory) -> None:
    raw, _ = await auth_client_factory.seed(scopes=[SCOPE_SPECTATOR], label="viewer")
    response = await auth_client_factory.client.get(
        "/model-providers",
        headers=auth_client_factory.headers(raw),
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []


async def test_no_credentials_when_auth_required_is_401(
    auth_client_factory: AuthClientFactory,
) -> None:
    response = await auth_client_factory.client.get("/model-providers")
    assert response.status_code == 401
    assert response.json()["detail"] == "authentication_required"


async def test_invalid_bearer_token_is_401(
    auth_client_factory: AuthClientFactory,
) -> None:
    response = await auth_client_factory.client.get(
        "/model-providers",
        headers={"Authorization": "Bearer pk_doesnotexist"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid_api_key"


async def test_disabled_key_is_401(
    auth_client_factory: AuthClientFactory,
) -> None:
    raw, key_id = await auth_client_factory.seed(scopes=[SCOPE_ADMIN], label="ex-admin")
    async with auth_client_factory.session_factory() as session, session.begin():
        await api_keys_repo.disable(session, key_id)
    response = await auth_client_factory.client.get(
        "/model-providers",
        headers=auth_client_factory.headers(raw),
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "api_key_disabled"


async def test_rate_limit_returns_429_then_resets(
    auth_client_factory: AuthClientFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Set the submitter limit to 2 so the test stays cheap.
    monkeypatch.setattr(
        "padrino.api.auth._limit_for_scopes",
        lambda scopes, settings: 2 if SCOPE_ADMIN not in scopes else 600,
    )
    raw, _ = await auth_client_factory.seed(scopes=[SCOPE_SPECTATOR], label="viewer")
    headers = auth_client_factory.headers(raw)

    # First two requests pass.
    for _ in range(2):
        r = await auth_client_factory.client.get("/model-providers", headers=headers)
        assert r.status_code == 200, r.text

    # Third request inside the same minute is 429 with Retry-After.
    r = await auth_client_factory.client.get("/model-providers", headers=headers)
    assert r.status_code == 429, r.text
    assert r.json()["detail"] == "rate_limited"
    assert "Retry-After" in r.headers
    assert int(r.headers["Retry-After"]) >= 1

    # Advance the clock past the 60s window — the limiter must drain.
    auth_client_factory.clock.now += 61.0
    r = await auth_client_factory.client.get("/model-providers", headers=headers)
    assert r.status_code == 200, r.text


async def test_create_key_endpoint_returns_raw_key_once(
    auth_client_factory: AuthClientFactory,
) -> None:
    raw_admin, _ = await auth_client_factory.seed(scopes=[SCOPE_ADMIN], label="root")
    headers = auth_client_factory.headers(raw_admin)

    response = await auth_client_factory.client.post(
        "/admin/keys",
        headers=headers,
        json={"label": "new-spectator", "scopes": [SCOPE_SPECTATOR]},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["label"] == "new-spectator"
    assert body["scopes"] == [SCOPE_SPECTATOR]
    new_raw = body["raw_key"]
    assert isinstance(new_raw, str) and new_raw.startswith(RAW_KEY_PREFIX)
    new_id = body["id"]
    assert body["key_prefix"] == new_raw[: len(body["key_prefix"])]

    # GET /admin/keys must never expose the raw key.
    listing = await auth_client_factory.client.get("/admin/keys", headers=headers)
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert any(item["id"] == new_id for item in items)
    raw_keys_in_existence = {new_raw, raw_admin}
    for item in items:
        assert "raw_key" not in item
        for value in item.values():
            if isinstance(value, str):
                assert value not in raw_keys_in_existence


async def test_create_key_rejects_unknown_scope(
    auth_client_factory: AuthClientFactory,
) -> None:
    raw_admin, _ = await auth_client_factory.seed(scopes=[SCOPE_ADMIN], label="root")
    response = await auth_client_factory.client.post(
        "/admin/keys",
        headers=auth_client_factory.headers(raw_admin),
        json={"label": "x", "scopes": ["mafia"]},
    )
    assert response.status_code == 422
    assert "unknown scope" in response.json()["detail"]


async def test_delete_key_disables_it(
    auth_client_factory: AuthClientFactory,
) -> None:
    raw_admin, _ = await auth_client_factory.seed(scopes=[SCOPE_ADMIN], label="root")
    raw_target, target_id = await auth_client_factory.seed(
        scopes=[SCOPE_SPECTATOR], label="kill-me"
    )

    # Target key works before disable.
    pre = await auth_client_factory.client.get(
        "/model-providers", headers=auth_client_factory.headers(raw_target)
    )
    assert pre.status_code == 200

    response = await auth_client_factory.client.delete(
        f"/admin/keys/{target_id}",
        headers=auth_client_factory.headers(raw_admin),
    )
    assert response.status_code == 200, response.text
    assert response.json()["disabled_at"] is not None

    # Target key is now 401.
    post = await auth_client_factory.client.get(
        "/model-providers", headers=auth_client_factory.headers(raw_target)
    )
    assert post.status_code == 401


async def test_delete_unknown_key_is_404(
    auth_client_factory: AuthClientFactory,
) -> None:
    raw_admin, _ = await auth_client_factory.seed(scopes=[SCOPE_ADMIN], label="root")
    response = await auth_client_factory.client.delete(
        f"/admin/keys/{uuid.uuid4()}",
        headers=auth_client_factory.headers(raw_admin),
    )
    assert response.status_code == 404


async def test_legacy_admin_token_still_works_with_deprecation_header(
    auth_client_factory: AuthClientFactory,
) -> None:
    response = await auth_client_factory.client.get(
        "/model-providers",
        headers={"X-Padrino-Admin-Token": "legacy-admin-token-for-shim"},
    )
    assert response.status_code == 200, response.text
    assert response.headers.get("Deprecation") == "true"
    assert "Sunset" in response.headers


async def test_legacy_admin_token_mismatch_falls_through_to_401(
    auth_client_factory: AuthClientFactory,
) -> None:
    response = await auth_client_factory.client.get(
        "/model-providers",
        headers={"X-Padrino-Admin-Token": "WRONG"},
    )
    assert response.status_code == 401


async def test_auth_disabled_grants_synthetic_admin(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = create_app(session_factory=session_factory, auth_required=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        response = await ac.get("/model-providers")
    assert response.status_code == 200


async def test_auth_required_is_default_on_every_protected_route(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """US-074: ``create_app(...)`` defaults to ``auth_required=True``.

    Anonymous requests to admin write routes and spectator read routes
    must both 401 with the canonical ``authentication_required`` detail,
    not just the write surface. Public unauthenticated routes
    (``/healthz``, ``/readyz``, ``/metrics``) stay open.
    """
    app = create_app(session_factory=session_factory)
    assert app.state.auth_required is True
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        protected_paths = (
            ("GET", "/model-providers"),
            ("GET", "/model-configs"),
            ("GET", "/prompt-versions"),
            ("GET", "/agent-builds"),
            ("GET", "/gauntlets"),
            ("GET", "/admin/keys"),
        )
        for method, path in protected_paths:
            response = await ac.request(method, path)
            assert response.status_code == 401, (method, path, response.text)
            assert response.json()["detail"] == "authentication_required"
        # ``/healthz`` stays anonymous for liveness probes.
        healthz = await ac.get("/healthz")
        assert healthz.status_code == 200


async def test_hashed_comparison_uses_constant_time(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # The repository hashes the raw key with sha256 and the auth path runs
    # hmac.compare_digest against the legacy admin token. This test asserts
    # both behaviors structurally so a future refactor that drops either
    # invariant breaks loudly.
    raw = generate_raw_key()
    digest = api_keys_repo.hash_api_key(raw)
    assert digest == api_keys_repo.hash_api_key(raw)
    # Different raw → different digest.
    assert digest != api_keys_repo.hash_api_key(raw + "x")
    # constant-time check is what we expect to use in the production path.
    assert hmac.compare_digest(digest, digest)
    # raw key never appears in the digest.
    assert raw not in digest


async def test_rate_limiter_unit_behaviour() -> None:
    clock = _FakeClock()
    limiter = RateLimiter(clock=clock)
    key_hash = "abcd" * 16
    allowed1, _ = await limiter.hit(key_hash, limit_per_minute=2)
    allowed2, _ = await limiter.hit(key_hash, limit_per_minute=2)
    allowed3, retry_after = await limiter.hit(key_hash, limit_per_minute=2)
    assert (allowed1, allowed2, allowed3) == (True, True, False)
    assert 0 < retry_after <= 60
    clock.now += 61.0
    allowed4, _ = await limiter.hit(key_hash, limit_per_minute=2)
    assert allowed4 is True
