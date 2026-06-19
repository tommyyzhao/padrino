"""Tests for the one-tap consent + 16+ age gate (US-130).

Covers:
* a pre-consent human action is rejected with HTTP 412;
* recording the one-tap combined consent (TOS + Privacy + 16+) lets the action
  through;
* bumping a document version re-prompts (the stale consent no longer counts);
* the append-only ``human_consents`` table records one row per document kind and
  never stores a raw IP (only its sha256).

The action/chat POST channels themselves land in US-134/US-135; this story owns
the gate. To exercise it without those routes, we mount a tiny router whose
endpoint depends on a valid human session then calls the shared
``enforce_consent`` helper — exactly how the real action channel will gate.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from http.cookies import SimpleCookie

import pytest
import pytest_asyncio
from fastapi import APIRouter, Depends, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import _get_auth_settings
from padrino.api.deps import get_session
from padrino.api.human_auth import (
    HUMAN_SESSION_COOKIE,
    HumanPrincipalContext,
    require_human,
)
from padrino.api.human_consent import enforce_consent, hash_source_ip
from padrino.db.models import HumanConsent
from padrino.db.repositories import human_consents as consents_repo

_gate_router = APIRouter()


@_gate_router.post("/_test/human/act")
async def _gated_action(
    request: Request,
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """A stand-in for the US-134 action channel, gated by consent."""
    settings = _get_auth_settings(request)
    await enforce_consent(session, subject_principal_id=ctx.principal_id, settings=settings)
    return {"status": "accepted"}


@pytest.fixture(autouse=True)
def _stub_provider_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-cerebras-key")


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(session_factory=session_factory, auth_required=True)
    app.include_router(_gate_router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


def _guest_token(resp_headers: object) -> str:
    jar = SimpleCookie()
    for raw in resp_headers.get_list("set-cookie"):  # type: ignore[attr-defined]
        if raw.startswith(f"{HUMAN_SESSION_COOKIE}="):
            jar.load(raw)
    return jar[HUMAN_SESSION_COOKIE].value


@pytest_asyncio.fixture
async def guest_token(client: AsyncClient) -> str:
    resp = await client.post("/human/guest")
    assert resp.status_code == 201
    return _guest_token(resp.headers)


@pytest.mark.asyncio
async def test_pre_consent_action_rejected(client: AsyncClient, guest_token: str) -> None:
    resp = await client.post("/_test/human/act", cookies={HUMAN_SESSION_COOKIE: guest_token})
    assert resp.status_code == 412
    assert resp.json()["detail"] == "consent_required"


@pytest.mark.asyncio
async def test_post_consent_action_allowed(client: AsyncClient, guest_token: str) -> None:
    consent = await client.post("/human/consent", cookies={HUMAN_SESSION_COOKIE: guest_token})
    assert consent.status_code == 201
    assert consent.json()["consented"] is True

    resp = await client.post("/_test/human/act", cookies={HUMAN_SESSION_COOKIE: guest_token})
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"


@pytest.mark.asyncio
async def test_consent_status_endpoint(client: AsyncClient, guest_token: str) -> None:
    before = await client.get("/human/consent", cookies={HUMAN_SESSION_COOKIE: guest_token})
    assert before.status_code == 200
    assert before.json()["consented"] is False

    await client.post("/human/consent", cookies={HUMAN_SESSION_COOKIE: guest_token})

    after = await client.get("/human/consent", cookies={HUMAN_SESSION_COOKIE: guest_token})
    assert after.json()["consented"] is True


@pytest.mark.asyncio
async def test_version_bump_reprompts(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-cerebras-key")
    monkeypatch.setenv("PADRINO_CONSENT_TOS_VERSION", "v1")
    monkeypatch.setenv("PADRINO_CONSENT_PRIVACY_VERSION", "v1")
    monkeypatch.setenv("PADRINO_CONSENT_AGE_GATE_VERSION", "v1")
    from padrino.settings import get_settings

    get_settings.cache_clear()
    try:
        app = create_app(session_factory=session_factory, auth_required=True)
        app.include_router(_gate_router)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
            token = _guest_token((await ac.post("/human/guest")).headers)
            await ac.post("/human/consent", cookies={HUMAN_SESSION_COOKIE: token})
            ok = await ac.post("/_test/human/act", cookies={HUMAN_SESSION_COOKIE: token})
            assert ok.status_code == 200

            # Bump the TOS document version: the stale consent no longer counts.
            monkeypatch.setenv("PADRINO_CONSENT_TOS_VERSION", "v2")
            get_settings.cache_clear()
            app2 = create_app(session_factory=session_factory, auth_required=True)
            app2.include_router(_gate_router)
            async with AsyncClient(
                transport=ASGITransport(app=app2), base_url="http://testserver"
            ) as ac2:
                reprompt = await ac2.post("/_test/human/act", cookies={HUMAN_SESSION_COOKIE: token})
                assert reprompt.status_code == 412

                # Re-accepting at the new version clears the gate again.
                await ac2.post("/human/consent", cookies={HUMAN_SESSION_COOKIE: token})
                again = await ac2.post("/_test/human/act", cookies={HUMAN_SESSION_COOKIE: token})
                assert again.status_code == 200
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_consent_records_one_append_only_row_per_kind(
    client: AsyncClient,
    guest_token: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await client.post("/human/consent", cookies={HUMAN_SESSION_COOKIE: guest_token})
    async with session_factory() as session:
        rows = (await session.execute(select(HumanConsent))).scalars().all()
    kinds = {row.document_kind for row in rows}
    assert kinds == consents_repo.REQUIRED_DOCUMENT_KINDS
    assert len(rows) == 3
    # Append-only: a second tap appends a fresh set rather than mutating.
    await client.post("/human/consent", cookies={HUMAN_SESSION_COOKIE: guest_token})
    async with session_factory() as session:
        rows2 = (await session.execute(select(HumanConsent))).scalars().all()
    assert len(rows2) == 6


@pytest.mark.asyncio
async def test_consent_requires_session(client: AsyncClient) -> None:
    assert (await client.post("/human/consent")).status_code == 401
    assert (await client.get("/human/consent")).status_code == 401


@pytest.mark.asyncio
async def test_source_ip_is_hashed_never_raw() -> None:
    assert hash_source_ip(None) is None
    assert hash_source_ip("   ") is None
    digest = hash_source_ip("203.0.113.7")
    assert digest is not None
    assert digest != "203.0.113.7"
    assert len(digest) == 64
