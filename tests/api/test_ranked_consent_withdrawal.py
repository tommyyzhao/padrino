"""US-305: ranked consent withdrawal is append-only and recency-aware."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from http.cookies import SimpleCookie

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api import human_consent
from padrino.api.app import create_app
from padrino.api.human_auth import HUMAN_SESSION_COOKIE
from padrino.db.models import HumanConsent, Principal
from padrino.db.repositories import human_consents as consents_repo
from padrino.legal.documents import get_legal_document
from padrino.settings import Settings


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(session_factory=session_factory, auth_required=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as ac:
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


async def _create_principal(session: AsyncSession) -> Principal:
    principal = Principal(kind="account", display_name=None)
    session.add(principal)
    await session.flush()
    return principal


def _ranked_content_hash() -> str:
    document = get_legal_document("RANKED")
    assert document is not None
    return document.content_hash


@pytest.mark.asyncio
async def test_ranked_withdrawal_route_appends_marker_without_deleting_history(
    client: AsyncClient,
    guest_token: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    base = await client.post("/human/consent", cookies={HUMAN_SESSION_COOKIE: guest_token})
    ranked = await client.post("/human/ranked-consent", cookies={HUMAN_SESSION_COOKIE: guest_token})
    assert base.status_code == 201
    assert ranked.status_code == 201
    assert ranked.json()["consented"] is True

    withdrawal = await client.post(
        "/human/consent/ranked/withdraw",
        cookies={HUMAN_SESSION_COOKIE: guest_token},
    )

    assert withdrawal.status_code == 201, withdrawal.text
    assert withdrawal.json()["consented"] is False

    status = await client.get("/human/ranked-consent", cookies={HUMAN_SESSION_COOKIE: guest_token})
    assert status.status_code == 200
    assert status.json()["consented"] is False

    async with session_factory() as session:
        rows = (await session.execute(select(HumanConsent))).scalars().all()

    assert len(rows) == 8
    assert {row.document_kind for row in rows}.issuperset(
        consents_repo.REQUIRED_DOCUMENT_KINDS | consents_repo.REQUIRED_RANKED_DOCUMENT_KINDS
    )
    withdrawal_rows = [
        row for row in rows if row.document_kind == consents_repo.DOCUMENT_KIND_RANKED_WITHDRAWN
    ]
    assert len(withdrawal_rows) == 1
    assert withdrawal_rows[0].content_hash == _ranked_content_hash()

    reaccepted = await client.post(
        "/human/ranked-consent", cookies={HUMAN_SESSION_COOKIE: guest_token}
    )
    assert reaccepted.status_code == 201
    assert reaccepted.json()["consented"] is True

    async with session_factory() as session:
        rows_after_reaccept = (await session.execute(select(HumanConsent))).scalars().all()

    assert len(rows_after_reaccept) == 12
    assert (
        sum(
            row.document_kind == consents_repo.DOCUMENT_KIND_RANKED_WITHDRAWN
            for row in rows_after_reaccept
        )
        == 1
    )


@pytest.mark.asyncio
async def test_has_current_ranked_consent_requires_complete_acceptance_after_withdrawal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    settings = Settings(_env_file=None)
    async with session_factory() as session:
        principal = await _create_principal(session)
        await human_consent.record_consent(
            session,
            subject_principal_id=principal.id,
            settings=settings,
            accepted_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
        await human_consent.record_ranked_consent(
            session,
            subject_principal_id=principal.id,
            settings=settings,
            accepted_at=datetime(2026, 6, 2, tzinfo=UTC),
        )
        assert await human_consent.has_current_ranked_consent(
            session,
            subject_principal_id=principal.id,
            settings=settings,
        )

        await human_consent.record_ranked_withdrawal(
            session,
            subject_principal_id=principal.id,
            settings=settings,
            withdrawn_at=datetime(2026, 6, 3, tzinfo=UTC),
        )
        assert not await human_consent.has_current_ranked_consent(
            session,
            subject_principal_id=principal.id,
            settings=settings,
        )

        requirements = human_consent.required_ranked_consent_requirements(settings)
        one_kind = consents_repo.DOCUMENT_KIND_RANKED_PUBLIC_HANDLE
        await consents_repo.record_consent_kinds(
            session,
            subject_principal_id=principal.id,
            kinds={one_kind},
            versions={one_kind: requirements[one_kind].version_label},
            content_hashes={one_kind: requirements[one_kind].content_hash},
            accepted_at=datetime(2026, 6, 4, tzinfo=UTC),
        )
        assert not await human_consent.has_current_ranked_consent(
            session,
            subject_principal_id=principal.id,
            settings=settings,
        )

        await human_consent.record_ranked_consent(
            session,
            subject_principal_id=principal.id,
            settings=settings,
            accepted_at=datetime(2026, 6, 5, tzinfo=UTC),
        )
        assert await human_consent.has_current_ranked_consent(
            session,
            subject_principal_id=principal.id,
            settings=settings,
        )
