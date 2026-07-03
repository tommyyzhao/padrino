"""US-300: admin-ranked ban routes."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.auth import SCOPE_ADMIN, SCOPE_SPECTATOR, generate_raw_key
from padrino.api.human_auth import HUMAN_SESSION_COOKIE, generate_session_token
from padrino.core.enums import LeagueKind
from padrino.core.ranked_eligibility import (
    RankedConsentFacts,
    RankedEligibilityInputs,
    RankedEligibilityReason,
    evaluate_ranked_eligibility,
)
from padrino.db.models import RankedBan
from padrino.db.repositories import api_keys as api_keys_repo
from padrino.db.repositories import human_principals, oauth_identities, ranked_bans


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(session_factory=session_factory, auth_required=True)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _api_key(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    scopes: list[str],
) -> str:
    raw = generate_raw_key()
    async with session_factory() as session, session.begin():
        await api_keys_repo.create(session, raw_key=raw, scopes=scopes, label="test")
    return raw


def _headers(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


async def _account(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    subject: str = "subject",
    email: str | None = "ranked@example.com",
    verified: bool = True,
) -> uuid.UUID:
    async with session_factory() as session, session.begin():
        principal = await oauth_identities.find_or_create_account(
            session,
            provider="test-oauth",
            subject=subject,
            display_name="Ranked Player",
            email=email,
            email_verified=verified,
            now=datetime(2026, 6, 27, tzinfo=UTC),
        )
        return principal.id


async def _human_session(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    principal_id: uuid.UUID,
) -> str:
    raw = generate_session_token()
    now = datetime(2026, 6, 27, tzinfo=UTC)
    async with session_factory() as session, session.begin():
        await human_principals.create_session(
            session,
            principal_id=principal_id,
            raw_token=raw,
            kind=human_principals.SESSION_KIND_ACCOUNT,
            issued_at=now,
            expires_at=now + timedelta(hours=1),
        )
    return raw


@pytest.mark.asyncio
async def test_admin_can_issue_and_lift_ranked_ban(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw_admin = await _api_key(session_factory, scopes=[SCOPE_ADMIN])
    principal_id = await _account(session_factory)

    issued = await client.post(
        "/admin/ranked-bans",
        headers=_headers(raw_admin),
        json={
            "principal_id": str(principal_id),
            "kind": "ban",
            "reason": "manual_review",
        },
    )

    assert issued.status_code == 201, issued.text
    body = issued.json()
    assert body["principal_id"] == str(principal_id)
    assert body["kind"] == "ban"
    assert body["reason"] == "manual_review"
    assert body["email_hash"] is not None
    assert "email" not in body
    assert body["lifted_at"] is None

    now = datetime(2026, 6, 27, tzinfo=UTC)
    async with session_factory() as session:
        assert await ranked_bans.is_principal_ranked_banned(
            session, principal_id=principal_id, now=now
        )
    decision = evaluate_ranked_eligibility(
        RankedEligibilityInputs(
            ranked_human_rating_enabled=True,
            principal_kind="account",
            email_verified=True,
            has_current_base_consent=True,
            ranked_consent=RankedConsentFacts(has_current_ranked_consent=True),
            ranked_banned=True,
            league_kind=LeagueKind.HUMANS_INCLUDED,
            league_ranked=True,
        )
    )
    assert decision.allowed is False
    assert decision.reason is RankedEligibilityReason.RANKED_BANNED

    lifted = await client.post(
        f"/admin/ranked-bans/{body['id']}/lift",
        headers=_headers(raw_admin),
    )
    assert lifted.status_code == 200, lifted.text
    assert lifted.json()["lifted_at"] is not None
    async with session_factory() as session:
        assert not await ranked_bans.is_principal_ranked_banned(
            session, principal_id=principal_id, now=now + timedelta(seconds=1)
        )


@pytest.mark.asyncio
async def test_ranked_ban_admin_route_rejects_non_admin_and_human_cookie(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    principal_id = await _account(session_factory)
    spectator = await _api_key(session_factory, scopes=[SCOPE_SPECTATOR])
    human_cookie = await _human_session(session_factory, principal_id=principal_id)
    payload = {
        "principal_id": str(principal_id),
        "kind": "ban",
        "reason": "manual_review",
    }

    human_only = await client.post(
        "/admin/ranked-bans",
        json=payload,
        cookies={HUMAN_SESSION_COOKIE: human_cookie},
    )
    assert human_only.status_code == 401
    assert human_only.json()["detail"] == "authentication_required"

    non_admin = await client.post(
        "/admin/ranked-bans",
        headers=_headers(spectator),
        json=payload,
        cookies={HUMAN_SESSION_COOKIE: human_cookie},
    )
    assert non_admin.status_code == 403
    assert non_admin.json()["detail"] == "insufficient_scope"


@pytest.mark.asyncio
async def test_no_automatic_ranked_ban_writer_outside_admin_route() -> None:
    src_root = Path(__file__).resolve().parents[2] / "src" / "padrino"
    allowed = {
        src_root / "api" / "routes" / "admin.py",
        src_root / "db" / "repositories" / "ranked_bans.py",
        src_root / "db" / "models.py",
        src_root / "db" / "migrations" / "versions" / "0060_ranked_ban.py",
    }
    offenders: list[str] = []
    for path in src_root.rglob("*.py"):
        if path in allowed:
            continue
        text = path.read_text(encoding="utf-8")
        if "RankedBan(" in text or "issue_ban_for_principal(" in text:
            offenders.append(str(path.relative_to(src_root)))

    assert offenders == []


@pytest.mark.asyncio
async def test_ranked_ban_route_does_not_store_raw_email(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    raw_admin = await _api_key(session_factory, scopes=[SCOPE_ADMIN])
    principal_id = await _account(
        session_factory,
        subject="raw-email-check",
        email="Secret.Email@Example.com",
    )

    response = await client.post(
        "/admin/ranked-bans",
        headers=_headers(raw_admin),
        json={"principal_id": str(principal_id), "kind": "ban", "reason": "manual_review"},
    )

    assert response.status_code == 201, response.text
    async with session_factory() as session:
        row = (await session.execute(select(RankedBan))).scalar_one()
    serialized = repr(row.__dict__)
    assert "Secret.Email@Example.com" not in serialized
    assert "secret.email@example.com" not in serialized
