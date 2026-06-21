"""Tests for browser-human principal/session auth (US-127).

Covers guest/account resolution, expired/revoked sessions, the guest-on-an
account-only route (403), and provable non-overlap with API-key auth in both
directions (a guest cookie grants zero API scope; an API key grants zero human
identity).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import APIRouter, Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import generate_raw_key, require_read
from padrino.api.human_auth import (
    HUMAN_SESSION_COOKIE,
    HumanPrincipalContext,
    generate_session_token,
    require_account,
    require_human,
)
from padrino.db.repositories import api_keys as api_keys_repo
from padrino.db.repositories import human_principals as principals_repo


@pytest.fixture(autouse=True)
def _stub_provider_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-cerebras-key")


def _build_test_app(session_factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    app = create_app(session_factory=session_factory, auth_required=True)

    router = APIRouter()

    @router.get("/_test/human")
    async def human_route(
        ctx: HumanPrincipalContext = Depends(require_human),
    ) -> dict[str, str]:
        return {"principal_id": str(ctx.principal_id), "kind": ctx.kind}

    @router.get("/_test/account")
    async def account_route(
        ctx: HumanPrincipalContext = Depends(require_account),
    ) -> dict[str, str]:
        return {"principal_id": str(ctx.principal_id)}

    @router.get("/_test/apikey")
    async def apikey_route(_: object = Depends(require_read)) -> dict[str, str]:
        return {"ok": "true"}

    app.include_router(router)
    return app


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = _build_test_app(session_factory)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _make_session(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    principal_kind: str,
    session_kind: str,
    expires_in: timedelta = timedelta(hours=1),
    revoked: bool = False,
    deleted_principal: bool = False,
) -> str:
    raw = generate_session_token()
    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        principal = await principals_repo.create_principal(
            session, kind=principal_kind, display_name="Tester"
        )
        if deleted_principal:
            principal.deleted_at = now
        record = await principals_repo.create_session(
            session,
            principal_id=principal.id,
            raw_token=raw,
            kind=session_kind,
            issued_at=now,
            expires_at=now + expires_in,
        )
        if revoked:
            record.revoked_at = now
    return raw


@pytest.mark.asyncio
async def test_guest_session_resolves(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _make_session(session_factory, principal_kind="guest", session_kind="guest")
    resp = await client.get("/_test/human", cookies={HUMAN_SESSION_COOKIE: token})
    assert resp.status_code == 200
    assert resp.json()["kind"] == "guest"


@pytest.mark.asyncio
async def test_account_session_resolves_on_human_and_account_routes(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _make_session(session_factory, principal_kind="account", session_kind="account")
    human = await client.get("/_test/human", cookies={HUMAN_SESSION_COOKIE: token})
    assert human.status_code == 200
    account = await client.get("/_test/account", cookies={HUMAN_SESSION_COOKIE: token})
    assert account.status_code == 200


@pytest.mark.asyncio
async def test_no_cookie_is_401_on_human_route(client: AsyncClient) -> None:
    resp = await client.get("/_test/human")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_unknown_token_is_401(client: AsyncClient) -> None:
    resp = await client.get("/_test/human", cookies={HUMAN_SESSION_COOKIE: "not-a-real-token"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_expired_session_is_401(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _make_session(
        session_factory,
        principal_kind="guest",
        session_kind="guest",
        expires_in=timedelta(seconds=-1),
    )
    resp = await client.get("/_test/human", cookies={HUMAN_SESSION_COOKIE: token})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_revoked_session_is_401(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _make_session(
        session_factory, principal_kind="guest", session_kind="guest", revoked=True
    )
    resp = await client.get("/_test/human", cookies={HUMAN_SESSION_COOKIE: token})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_deleted_principal_is_401(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _make_session(
        session_factory,
        principal_kind="guest",
        session_kind="guest",
        deleted_principal=True,
    )
    resp = await client.get("/_test/human", cookies={HUMAN_SESSION_COOKIE: token})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_guest_on_account_only_route_is_403(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    token = await _make_session(session_factory, principal_kind="guest", session_kind="guest")
    resp = await client.get("/_test/account", cookies={HUMAN_SESSION_COOKIE: token})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_guest_cookie_grants_zero_api_scope(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A guest session cookie must not authenticate an API-key-scoped route."""
    token = await _make_session(session_factory, principal_kind="guest", session_kind="guest")
    resp = await client.get("/_test/apikey", cookies={HUMAN_SESSION_COOKIE: token})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_api_key_grants_zero_human_identity(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A valid API key (Bearer) must not authenticate a human-only route."""
    raw = generate_raw_key()
    async with session_factory() as session, session.begin():
        await api_keys_repo.create(session, raw_key=raw, scopes=["spectator"], label="t")
    # The api key authenticates the api-key route...
    ok = await client.get("/_test/apikey", headers={"Authorization": f"Bearer {raw}"})
    assert ok.status_code == 200
    # ...but grants zero human identity on a human-only route.
    resp = await client.get("/_test/human", headers={"Authorization": f"Bearer {raw}"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_session_token_persisted_only_as_hash(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The plaintext token never appears in human_sessions; only its sha256."""
    raw = generate_session_token()
    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        principal = await principals_repo.create_principal(session, kind="guest")
        record = await principals_repo.create_session(
            session,
            principal_id=principal.id,
            raw_token=raw,
            kind="guest",
            issued_at=now,
            expires_at=now + timedelta(hours=1),
        )
        assert record.session_hash == principals_repo.hash_session_token(raw)
        assert record.session_hash != raw


@pytest.mark.asyncio
async def test_occupant_principal_id_links_seat_to_principal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A game seat can carry a nullable occupant_principal_id FK to a principal."""
    from padrino.db.models import Game, GameSeat

    async with session_factory() as session, session.begin():
        principal = await principals_repo.create_principal(session, kind="guest")
        game = Game(
            ruleset_id="mini7_v1",
            game_seed="seed",
            status="PENDING",
        )
        session.add(game)
        await session.flush()
        seat = GameSeat(
            game_id=game.id,
            public_player_id="P01",
            seat_index=0,
            agent_build_id=None,
            seat_kind="HUMAN",
            role="VILLAGER",
            faction="TOWN",
            alive=True,
            occupant_principal_id=principal.id,
        )
        session.add(seat)
        await session.flush()
        seat_id = seat.id

    async with session_factory() as session:
        loaded = await session.get(GameSeat, seat_id)
        assert loaded is not None
        assert loaded.occupant_principal_id == principal.id
        assert loaded.agent_build_id is None
        assert loaded.seat_kind == "HUMAN"
