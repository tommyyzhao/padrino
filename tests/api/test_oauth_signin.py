"""Tests for optional OAuth sign-in, one provider (US-129).

The provider network round-trip is stubbed end-to-end (``set_resolve_user_info``)
so no live provider is contacted. Covers: the start redirect carries CSRF state +
PKCE, the callback validates state and find-or-creates an account principal keyed
by (provider, subject), a repeat sign-in resolves the SAME account, a signed-in
guest is upgraded in place (its sessions re-point to the account), the routes 503
when no provider is configured, and provider secrets/tokens are never persisted.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from http.cookies import SimpleCookie

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.human_auth import HUMAN_SESSION_COOKIE
from padrino.api.oauth import (
    OAuthUserInfo,
    reset_resolve_user_info,
    set_resolve_user_info,
)
from padrino.api.routes.human import OAUTH_STATE_COOKIE, OAUTH_VERIFIER_COOKIE
from padrino.db.models import OAuthIdentity, Principal
from padrino.settings import get_settings

_OAUTH_ENV = {
    "PADRINO_OAUTH_PROVIDER": "google",
    "PADRINO_OAUTH_CLIENT_ID": "client-id-123",
    "PADRINO_OAUTH_CLIENT_SECRET": "super-secret",
    "PADRINO_OAUTH_AUTHORIZE_URL": "https://provider.example/authorize",
    "PADRINO_OAUTH_TOKEN_URL": "https://provider.example/token",
    "PADRINO_OAUTH_USERINFO_URL": "https://provider.example/userinfo",
    "PADRINO_OAUTH_REDIRECT_URL": "https://app.example/human/oauth/google/callback",
    "PADRINO_HUMAN_SESSION_COOKIE_SECURE": "false",
    "CEREBRAS_API_KEY": "test-cerebras-key",
}


@pytest.fixture(autouse=True)
def _oauth_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key, value in _OAUTH_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _restore_resolver() -> Iterator[None]:
    yield
    reset_resolve_user_info()


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(
        session_factory=session_factory,
        auth_required=True,
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _cookie(resp_headers: object, name: str) -> str | None:
    jar = SimpleCookie()
    for raw in resp_headers.get_list("set-cookie"):  # type: ignore[attr-defined]
        if raw.startswith(f"{name}="):
            jar.load(raw)
    if name not in jar:
        return None
    return jar[name].value


def _stub_subject(subject: str, *, name: str | None = "Alice") -> None:
    async def _resolve(config: object, *, code: str, code_verifier: str) -> OAuthUserInfo:
        return OAuthUserInfo(subject=subject, display_name=name)

    set_resolve_user_info(_resolve)


@pytest.mark.asyncio
async def test_start_redirects_with_state_and_pkce(client: AsyncClient) -> None:
    resp = await client.get("/human/oauth/google/start")
    assert resp.status_code == 307
    location = resp.headers["location"]
    assert location.startswith("https://provider.example/authorize")
    assert "state=" in location
    assert "code_challenge=" in location
    assert "code_challenge_method=S256" in location
    assert _cookie(resp.headers, OAUTH_STATE_COOKIE) is not None
    assert _cookie(resp.headers, OAUTH_VERIFIER_COOKIE) is not None


@pytest.mark.asyncio
async def test_callback_creates_account_keyed_by_subject(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    start = await client.get("/human/oauth/google/start")
    state = _cookie(start.headers, OAUTH_STATE_COOKIE)
    verifier = _cookie(start.headers, OAUTH_VERIFIER_COOKIE)
    assert state is not None and verifier is not None

    _stub_subject("subject-abc")
    resp = await client.get(
        "/human/oauth/google/callback",
        params={"code": "auth-code", "state": state},
        cookies={OAUTH_STATE_COOKIE: state, OAUTH_VERIFIER_COOKIE: verifier},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "account"
    assert _cookie(resp.headers, HUMAN_SESSION_COOKIE) is not None

    async with session_factory() as session:
        identities = (await session.execute(select(OAuthIdentity))).scalars().all()
        principals = (await session.execute(select(Principal))).scalars().all()
    assert len(identities) == 1
    assert identities[0].provider == "google"
    assert identities[0].subject == "subject-abc"
    # The provider secret/token is never stored anywhere on the identity row.
    assert "super-secret" not in repr(identities[0].__dict__)
    assert len(principals) == 1
    assert principals[0].kind == "account"


@pytest.mark.asyncio
async def test_repeat_signin_resolves_same_account(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    async def _login() -> str:
        start = await client.get("/human/oauth/google/start")
        state = _cookie(start.headers, OAUTH_STATE_COOKIE)
        verifier = _cookie(start.headers, OAUTH_VERIFIER_COOKIE)
        assert state is not None and verifier is not None
        _stub_subject("subject-repeat")
        resp = await client.get(
            "/human/oauth/google/callback",
            params={"code": "auth-code", "state": state},
            cookies={OAUTH_STATE_COOKIE: state, OAUTH_VERIFIER_COOKIE: verifier},
        )
        assert resp.status_code == 200
        return str(resp.json()["principal_id"])

    first = await _login()
    second = await _login()
    assert first == second

    async with session_factory() as session:
        principals = (await session.execute(select(Principal))).scalars().all()
        identities = (await session.execute(select(OAuthIdentity))).scalars().all()
    assert len(principals) == 1
    assert len(identities) == 1


@pytest.mark.asyncio
async def test_signed_in_guest_upgraded_in_place(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    guest = await client.post("/human/guest")
    guest_token = _cookie(guest.headers, HUMAN_SESSION_COOKIE)
    guest_id = guest.json()["principal_id"]
    assert guest_token is not None

    start = await client.get("/human/oauth/google/start")
    state = _cookie(start.headers, OAUTH_STATE_COOKIE)
    verifier = _cookie(start.headers, OAUTH_VERIFIER_COOKIE)
    assert state is not None and verifier is not None

    _stub_subject("subject-upgrade")
    resp = await client.get(
        "/human/oauth/google/callback",
        params={"code": "auth-code", "state": state},
        cookies={
            OAUTH_STATE_COOKIE: state,
            OAUTH_VERIFIER_COOKIE: verifier,
            HUMAN_SESSION_COOKIE: guest_token,
        },
    )
    assert resp.status_code == 200
    # The SAME principal is upgraded in place (no new principal created).
    assert resp.json()["principal_id"] == guest_id
    assert resp.json()["kind"] == "account"

    async with session_factory() as session:
        principals = (await session.execute(select(Principal))).scalars().all()
    assert len(principals) == 1
    assert principals[0].kind == "account"

    # The original guest cookie still resolves (its session re-points in place).
    me = await client.get("/human/me", cookies={HUMAN_SESSION_COOKIE: guest_token})
    assert me.status_code == 200
    assert me.json()["kind"] == "account"


@pytest.mark.asyncio
async def test_callback_rejects_state_mismatch(client: AsyncClient) -> None:
    start = await client.get("/human/oauth/google/start")
    verifier = _cookie(start.headers, OAUTH_VERIFIER_COOKIE)
    assert verifier is not None
    _stub_subject("subject-x")
    resp = await client.get(
        "/human/oauth/google/callback",
        params={"code": "c", "state": "attacker-state"},
        cookies={OAUTH_STATE_COOKIE: "real-state", OAUTH_VERIFIER_COOKIE: verifier},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_routes_503_without_provider_config(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PADRINO_OAUTH_PROVIDER", raising=False)
    monkeypatch.delenv("PADRINO_OAUTH_CLIENT_ID", raising=False)
    get_settings.cache_clear()
    try:
        app = create_app(
            session_factory=session_factory,
            auth_required=True,
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
            start = await ac.get("/human/oauth/google/start")
            assert start.status_code == 503
            cb = await ac.get("/human/oauth/google/callback", params={"code": "c", "state": "s"})
            assert cb.status_code == 503
    finally:
        get_settings.cache_clear()
