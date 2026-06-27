"""US-304: purpose-segmented ranked consent gate tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from http.cookies import SimpleCookie

import pytest
import pytest_asyncio
from fastapi import APIRouter, Depends, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api import human_consent
from padrino.api.app import create_app
from padrino.api.auth import _get_auth_settings
from padrino.api.deps import get_session
from padrino.api.human_auth import HUMAN_SESSION_COOKIE, HumanPrincipalContext, require_human
from padrino.db.models import HumanConsent, Principal
from padrino.db.repositories import human_consents as consents_repo
from padrino.legal.documents import LegalDocument, get_legal_document, list_legal_documents
from padrino.settings import Settings

_ranked_gate_router = APIRouter()


@_ranked_gate_router.post("/_test/human/ranked-play")
async def _ranked_gated_action(
    request: Request,
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    """A stand-in for future ranked admission, gated only by ranked consent."""
    settings = _get_auth_settings(request)
    await human_consent.enforce_ranked_consent(
        session, subject_principal_id=ctx.principal_id, settings=settings
    )
    return {"status": "ranked_accepted"}


@pytest.fixture(autouse=True)
def _stub_provider_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CEREBRAS_API_KEY", "test-cerebras-key")


@pytest_asyncio.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    app = create_app(session_factory=session_factory, auth_required=True)
    app.include_router(_ranked_gate_router)
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


def _ranked_document() -> LegalDocument:
    document = get_legal_document("RANKED")
    assert document is not None
    return document


@pytest.mark.asyncio
async def test_ranked_consent_route_records_four_purpose_rows(
    client: AsyncClient,
    guest_token: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    resp = await client.post("/human/ranked-consent", cookies={HUMAN_SESSION_COOKIE: guest_token})

    assert resp.status_code == 201, resp.text
    payload = resp.json()
    assert payload["consented"] is False
    assert set(payload["required_versions"]) == consents_repo.REQUIRED_RANKED_DOCUMENT_KINDS

    ranked_doc = _ranked_document()
    async with session_factory() as session:
        rows = (await session.execute(select(HumanConsent))).scalars().all()

    assert len(rows) == 4
    assert {row.document_kind for row in rows} == consents_repo.REQUIRED_RANKED_DOCUMENT_KINDS
    for row in rows:
        assert row.document_version == ranked_doc.version_label
        assert row.content_hash == ranked_doc.content_hash


@pytest.mark.asyncio
async def test_base_consent_alone_does_not_satisfy_ranked_gate(
    client: AsyncClient,
    guest_token: str,
) -> None:
    base = await client.post("/human/consent", cookies={HUMAN_SESSION_COOKIE: guest_token})
    assert base.status_code == 201
    assert base.json()["consented"] is True

    ranked_gate = await client.post(
        "/_test/human/ranked-play", cookies={HUMAN_SESSION_COOKIE: guest_token}
    )
    assert ranked_gate.status_code == 412
    assert ranked_gate.json()["detail"] == "ranked_consent_required"

    ranked = await client.post("/human/ranked-consent", cookies={HUMAN_SESSION_COOKIE: guest_token})
    assert ranked.status_code == 201
    assert ranked.json()["consented"] is True

    accepted = await client.post(
        "/_test/human/ranked-play", cookies={HUMAN_SESSION_COOKIE: guest_token}
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "ranked_accepted"


@pytest.mark.asyncio
async def test_each_ranked_purpose_is_independently_required(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        principal = await _create_principal(session)
        await human_consent.record_consent(
            session,
            subject_principal_id=principal.id,
            settings=Settings(),
            accepted_at=datetime(2026, 6, 27, tzinfo=UTC),
        )
        requirements = human_consent.required_ranked_consent_requirements(Settings())
        missing_kind = consents_repo.DOCUMENT_KIND_RANKED_ANTICHEAT_PROFILING
        partial_kinds = consents_repo.REQUIRED_RANKED_DOCUMENT_KINDS - {missing_kind}
        await consents_repo.record_consent_kinds(
            session,
            subject_principal_id=principal.id,
            kinds=partial_kinds,
            versions={kind: requirements[kind].version_label for kind in partial_kinds},
            content_hashes={kind: requirements[kind].content_hash for kind in partial_kinds},
            accepted_at=datetime(2026, 6, 27, tzinfo=UTC),
        )

        assert not await human_consent.has_current_ranked_consent(
            session,
            subject_principal_id=principal.id,
            settings=Settings(),
        )

        await consents_repo.record_consent_kinds(
            session,
            subject_principal_id=principal.id,
            kinds={missing_kind},
            versions={missing_kind: requirements[missing_kind].version_label},
            content_hashes={missing_kind: requirements[missing_kind].content_hash},
            accepted_at=datetime(2026, 6, 27, tzinfo=UTC),
        )

        assert await human_consent.has_current_ranked_consent(
            session,
            subject_principal_id=principal.id,
            settings=Settings(),
        )


@pytest.mark.asyncio
async def test_ranked_document_body_change_reprompts_only_ranked_consent(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        principal = await _create_principal(session)
        await human_consent.record_consent(
            session,
            subject_principal_id=principal.id,
            settings=Settings(),
            accepted_at=datetime(2026, 6, 27, tzinfo=UTC),
        )
        await human_consent.record_ranked_consent(
            session,
            subject_principal_id=principal.id,
            settings=Settings(),
            accepted_at=datetime(2026, 6, 27, tzinfo=UTC),
        )
        assert await human_consent.has_current_consent(
            session,
            subject_principal_id=principal.id,
            settings=Settings(),
        )
        assert await human_consent.has_current_ranked_consent(
            session,
            subject_principal_id=principal.id,
            settings=Settings(),
        )

        changed_documents = tuple(
            LegalDocument(
                kind=document.kind,
                version_label=document.version_label,
                body=f"{document.body}\nRanked purposes changed.\n",
            )
            if document.kind == "RANKED"
            else document
            for document in list_legal_documents()
        )
        monkeypatch.setattr(human_consent, "list_legal_documents", lambda: changed_documents)

        assert await human_consent.has_current_consent(
            session,
            subject_principal_id=principal.id,
            settings=Settings(),
        )
        assert not await human_consent.has_current_ranked_consent(
            session,
            subject_principal_id=principal.id,
            settings=Settings(),
        )
