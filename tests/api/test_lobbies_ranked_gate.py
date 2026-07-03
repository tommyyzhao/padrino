"""US-291: ranked lobby creation is gated by server-resolved eligibility."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api.app import create_app
from padrino.api.human_auth import HUMAN_SESSION_COOKIE, generate_session_token
from padrino.api.human_consent import record_consent, record_ranked_consent
from padrino.api.ranked_eligibility import resolve_ranked_eligibility
from padrino.core.ranked_ban import RankedBanKind
from padrino.core.ranked_eligibility import RankedEligibilityReason, evaluate_ranked_eligibility
from padrino.db.models import League, Lobby, Principal
from padrino.db.repositories import human_principals, oauth_identities, ranked_bans
from padrino.settings import Settings

_NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def ranked_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(session_factory=session_factory, auth_required=True)
    app.state.auth_settings = Settings(
        _env_file=None,
        padrino_ranked_human_rating_enabled=True,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
        yield ac


@pytest_asyncio.fixture
async def disabled_client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(session_factory=session_factory, auth_required=True)
    app.state.auth_settings = Settings(
        _env_file=None,
        padrino_ranked_human_rating_enabled=False,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
        yield ac


async def _guest_token(client: AsyncClient) -> str:
    response = await client.post("/human/guest")
    assert response.status_code == 201, response.text
    return response.cookies[HUMAN_SESSION_COOKIE]


async def _account_with_session(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    email_verified: bool = True,
    consent: bool = True,
    ranked_consent: bool = True,
    banned: bool = False,
    deleted: bool = False,
) -> str:
    raw_token = generate_session_token()
    settings = Settings(_env_file=None, padrino_ranked_human_rating_enabled=True)
    async with session_factory() as session, session.begin():
        principal = await oauth_identities.find_or_create_account(
            session,
            provider="test-oauth",
            subject=f"ranked-{uuid.uuid4()}",
            display_name="Ranked Player",
            email="ranked@example.com",
            email_verified=email_verified,
            now=_NOW,
        )
        await human_principals.create_session(
            session,
            principal_id=principal.id,
            raw_token=raw_token,
            kind=human_principals.SESSION_KIND_ACCOUNT,
            issued_at=_NOW,
            expires_at=_NOW + timedelta(days=365),
        )
        if consent:
            await record_consent(
                session,
                subject_principal_id=principal.id,
                settings=settings,
                accepted_at=_NOW,
            )
        if ranked_consent:
            await record_ranked_consent(
                session,
                subject_principal_id=principal.id,
                settings=settings,
                accepted_at=_NOW,
            )
        if banned:
            await ranked_bans.issue_ban_for_principal(
                session,
                principal_id=principal.id,
                kind=RankedBanKind.BAN,
                reason="manual_review",
                created_by="test",
                now=_NOW,
            )
        if deleted:
            principal.deleted_at = _NOW
            principal.updated_at = _NOW
    return raw_token


async def _league_count(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as session:
        return (await session.execute(select(func.count()).select_from(League))).scalar_one()


async def _lobby_count(session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_factory() as session:
        return (await session.execute(select(func.count()).select_from(Lobby))).scalar_one()


@pytest.mark.asyncio
async def test_resolve_ranked_eligibility_is_read_only_and_guest_first(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(_env_file=None, padrino_ranked_human_rating_enabled=True)
    async with session_factory() as session, session.begin():
        guest = await human_principals.create_principal(
            session,
            kind=human_principals.PRINCIPAL_KIND_GUEST,
            display_name="Guest",
        )
        await record_consent(
            session,
            subject_principal_id=guest.id,
            settings=settings,
            accepted_at=_NOW,
        )
        await record_ranked_consent(
            session,
            subject_principal_id=guest.id,
            settings=settings,
            accepted_at=_NOW,
        )
        await ranked_bans.issue_ban_for_principal(
            session,
            principal_id=guest.id,
            kind=RankedBanKind.BAN,
            reason="manual_review",
            created_by="test",
            now=_NOW,
        )
        before_counts = {
            "principals": (
                await session.execute(select(func.count()).select_from(Principal))
            ).scalar_one(),
            "leagues": (
                await session.execute(select(func.count()).select_from(League))
            ).scalar_one(),
        }

        facts = await resolve_ranked_eligibility(
            session,
            principal_id=guest.id,
            settings=settings,
            now=_NOW,
        )

        after_counts = {
            "principals": (
                await session.execute(select(func.count()).select_from(Principal))
            ).scalar_one(),
            "leagues": (
                await session.execute(select(func.count()).select_from(League))
            ).scalar_one(),
        }

    assert before_counts == after_counts
    assert facts.principal_kind == human_principals.PRINCIPAL_KIND_GUEST
    decision = evaluate_ranked_eligibility(facts)
    assert decision.allowed is False
    assert decision.reason is RankedEligibilityReason.GUEST_PRINCIPAL


@pytest.mark.asyncio
async def test_resolve_ranked_eligibility_soft_deleted_principal_fails_closed(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(_env_file=None, padrino_ranked_human_rating_enabled=True)
    async with session_factory() as session, session.begin():
        principal = await oauth_identities.find_or_create_account(
            session,
            provider="test-oauth",
            subject="deleted-ranked-user",
            display_name="Deleted",
            email="deleted@example.com",
            email_verified=True,
            now=_NOW,
        )
        principal.deleted_at = _NOW
        facts = await resolve_ranked_eligibility(
            session,
            principal_id=principal.id,
            settings=settings,
            now=_NOW,
        )

    decision = evaluate_ranked_eligibility(facts)
    assert decision.allowed is False
    assert decision.reason is RankedEligibilityReason.GUEST_PRINCIPAL


@pytest.mark.asyncio
async def test_ranked_lobby_flag_off_returns_reason_and_writes_no_rows(
    disabled_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = await _account_with_session(session_factory)
    before_leagues = await _league_count(session_factory)
    before_lobbies = await _lobby_count(session_factory)

    response = await disabled_client.post(
        "/lobbies",
        json={
            "ruleset_id": "mini7_v1",
            "ranked": True,
            "integrity_acknowledged": True,
        },
        cookies={HUMAN_SESSION_COOKIE: token},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == RankedEligibilityReason.RANKED_DISABLED.value
    assert await _league_count(session_factory) == before_leagues
    assert await _lobby_count(session_factory) == before_lobbies


@pytest.mark.asyncio
async def test_ranked_lobby_guest_denied_with_machine_reason(
    ranked_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = await _guest_token(ranked_client)
    before_leagues = await _league_count(session_factory)

    response = await ranked_client.post(
        "/lobbies",
        json={
            "ruleset_id": "mini7_v1",
            "ranked": True,
            "integrity_acknowledged": True,
        },
        cookies={HUMAN_SESSION_COOKIE: token},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == RankedEligibilityReason.GUEST_PRINCIPAL.value
    assert await _league_count(session_factory) == before_leagues


@pytest.mark.asyncio
async def test_ranked_lobby_verified_consented_user_creates_ranked_league(
    ranked_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = await _account_with_session(session_factory)

    response = await ranked_client.post(
        "/lobbies",
        json={
            "ruleset_id": "mini7_v1",
            "ranked": True,
            "integrity_acknowledged": True,
        },
        cookies={HUMAN_SESSION_COOKIE: token},
    )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["ranked"] is True
    assert payload["integrity_acknowledged"] is True
    async with session_factory() as session:
        league = await session.get(League, uuid.UUID(payload["league_id"]))
    assert league is not None
    assert league.ranked is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"email_verified": False}, RankedEligibilityReason.EMAIL_UNVERIFIED),
        ({"consent": False}, RankedEligibilityReason.BASE_CONSENT_MISSING),
        ({"ranked_consent": False}, RankedEligibilityReason.RANKED_CONSENT_MISSING),
        ({"banned": True}, RankedEligibilityReason.RANKED_BANNED),
    ],
)
async def test_ranked_lobby_denials_return_predicate_reason_tokens(
    ranked_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    kwargs: dict[str, bool],
    reason: RankedEligibilityReason,
) -> None:
    token = await _account_with_session(session_factory, **kwargs)
    before_leagues = await _league_count(session_factory)

    response = await ranked_client.post(
        "/lobbies",
        json={
            "ruleset_id": "mini7_v1",
            "ranked": True,
            "integrity_acknowledged": True,
        },
        cookies={HUMAN_SESSION_COOKIE: token},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == reason.value
    assert await _league_count(session_factory) == before_leagues


@pytest.mark.asyncio
async def test_ranked_integrity_ack_is_checked_before_eligibility(
    ranked_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = await _account_with_session(session_factory, consent=False, ranked_consent=False)

    response = await ranked_client.post(
        "/lobbies",
        json={
            "ruleset_id": "mini7_v1",
            "ranked": True,
            "integrity_acknowledged": False,
        },
        cookies={HUMAN_SESSION_COOKIE: token},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "integrity_acknowledgement_required"


@pytest.mark.asyncio
async def test_casual_lobby_create_unaffected_for_guest_and_unverified_account(
    ranked_client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    guest = await _guest_token(ranked_client)
    unverified = await _account_with_session(
        session_factory,
        email_verified=False,
        consent=False,
        ranked_consent=False,
    )

    guest_response = await ranked_client.post(
        "/lobbies",
        json={"ruleset_id": "mini7_v1", "ranked": False},
        cookies={HUMAN_SESSION_COOKIE: guest},
    )
    account_response = await ranked_client.post(
        "/lobbies",
        json={"ruleset_id": "mini7_v1", "ranked": False},
        cookies={HUMAN_SESSION_COOKIE: unverified},
    )

    assert guest_response.status_code == 201, guest_response.text
    assert account_response.status_code == 201, account_response.text
    assert guest_response.json()["ranked"] is False
    assert account_response.json()["ranked"] is False
