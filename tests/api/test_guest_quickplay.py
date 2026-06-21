"""Tests for guest quickplay (US-128).

Covers ``POST /human/guest`` (guest principal + session cookie creation),
the cookie security attributes, ``PATCH /human/me`` / ``GET /human/me`` for the
display name, reachability under ``auth_required=True``, the fact that creating
a guest never touches ``api_keys`` and grants no API scope, and that the
plaintext token never appears in any DB column (only its sha256).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from http.cookies import SimpleCookie

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.human_auth import HUMAN_SESSION_COOKIE
from padrino.db.models import ApiKey, HumanSession, Principal
from padrino.db.repositories import human_principals as principals_repo


@pytest.fixture(autouse=True)
def _stub_provider_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-cerebras-key")


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(session_factory=session_factory, auth_required=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _set_cookie_header(resp_headers: object, name: str) -> SimpleCookie:
    """Parse the matching ``Set-Cookie`` header into a ``SimpleCookie``."""
    jar = SimpleCookie()
    for raw in resp_headers.get_list("set-cookie"):  # type: ignore[attr-defined]
        if raw.startswith(f"{name}="):
            jar.load(raw)
    assert name in jar, f"no Set-Cookie for {name!r}"
    return jar


@pytest.mark.asyncio
async def test_create_guest_returns_summary_and_is_reachable_with_auth_required(
    client: AsyncClient,
) -> None:
    resp = await client.post("/human/guest")
    assert resp.status_code == 201
    body = resp.json()
    assert body["kind"] == "guest"
    assert "principal_id" in body
    assert body["display_name"] is None


@pytest.mark.asyncio
async def test_guest_cookie_attributes(client: AsyncClient) -> None:
    resp = await client.post("/human/guest")
    jar = _set_cookie_header(resp.headers, HUMAN_SESSION_COOKIE)
    morsel = jar[HUMAN_SESSION_COOKIE]
    assert morsel["httponly"]
    assert morsel["samesite"].lower() == "lax"
    assert morsel["path"] == "/"
    # The opaque token is non-empty and is NOT the principal id.
    assert morsel.value
    assert morsel.value != resp.json()["principal_id"]


@pytest.mark.asyncio
async def test_guest_cookie_secure_flag_follows_settings(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-cerebras-key")
    monkeypatch.setenv("PADRINO_HUMAN_SESSION_COOKIE_SECURE", "false")
    from padrino.settings import get_settings

    get_settings.cache_clear()
    try:
        app = create_app(session_factory=session_factory, auth_required=True)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
            resp = await ac.post("/human/guest")
        jar = _set_cookie_header(resp.headers, HUMAN_SESSION_COOKIE)
        assert jar[HUMAN_SESSION_COOKIE]["secure"] == ""
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_set_and_get_display_name(client: AsyncClient) -> None:
    create = await client.post("/human/guest")
    token = _set_cookie_header(create.headers, HUMAN_SESSION_COOKIE)[HUMAN_SESSION_COOKIE].value

    patch = await client.patch(
        "/human/me",
        cookies={HUMAN_SESSION_COOKIE: token},
        json={"display_name": "Frank"},
    )
    assert patch.status_code == 200
    assert patch.json()["display_name"] == "Frank"

    me = await client.get("/human/me", cookies={HUMAN_SESSION_COOKIE: token})
    assert me.status_code == 200
    assert me.json()["display_name"] == "Frank"
    assert me.json()["kind"] == "guest"


@pytest.mark.asyncio
async def test_display_name_not_globally_unique(client: AsyncClient) -> None:
    a = await client.post("/human/guest")
    token_a = _set_cookie_header(a.headers, HUMAN_SESSION_COOKIE)[HUMAN_SESSION_COOKIE].value
    b = await client.post("/human/guest")
    token_b = _set_cookie_header(b.headers, HUMAN_SESSION_COOKIE)[HUMAN_SESSION_COOKIE].value

    r1 = await client.patch(
        "/human/me", cookies={HUMAN_SESSION_COOKIE: token_a}, json={"display_name": "Same"}
    )
    r2 = await client.patch(
        "/human/me", cookies={HUMAN_SESSION_COOKIE: token_b}, json={"display_name": "Same"}
    )
    assert r1.status_code == 200
    assert r2.status_code == 200


@pytest.mark.asyncio
async def test_display_name_validation_rejects_blank(client: AsyncClient) -> None:
    create = await client.post("/human/guest")
    token = _set_cookie_header(create.headers, HUMAN_SESSION_COOKIE)[HUMAN_SESSION_COOKIE].value
    resp = await client.patch(
        "/human/me", cookies={HUMAN_SESSION_COOKIE: token}, json={"display_name": "   "}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_me_requires_session(client: AsyncClient) -> None:
    resp = await client.get("/human/me")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_creating_guest_never_touches_api_keys(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await client.post("/human/guest")
    async with session_factory() as session:
        api_keys = (await session.execute(select(ApiKey))).scalars().all()
        principals = (await session.execute(select(Principal))).scalars().all()
    assert api_keys == []
    assert len(principals) == 1
    assert principals[0].kind == "guest"


@pytest.mark.asyncio
async def test_plaintext_token_never_persisted(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    resp = await client.post("/human/guest")
    token = _set_cookie_header(resp.headers, HUMAN_SESSION_COOKIE)[HUMAN_SESSION_COOKIE].value
    async with session_factory() as session:
        rows = (await session.execute(select(HumanSession))).scalars().all()
    assert len(rows) == 1
    stored = rows[0]
    assert stored.session_hash != token
    assert stored.session_hash == principals_repo.hash_session_token(token)
