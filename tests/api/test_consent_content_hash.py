"""Tests for content-hash-bound human consent (US-303)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from padrino.api import human_consent
from padrino.db.models import HumanConsent, Principal
from padrino.db.repositories import human_consents as consents_repo
from padrino.legal.documents import LegalDocument, get_legal_document, list_legal_documents
from padrino.settings import Settings


async def _create_principal(session: AsyncSession) -> Principal:
    principal = Principal(kind="guest", display_name=None)
    session.add(principal)
    await session.flush()
    return principal


def _current_document(kind: str) -> LegalDocument:
    document = get_legal_document(kind)
    assert document is not None
    return document


@pytest.mark.asyncio
async def test_record_consent_stores_registry_content_hash(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        principal = await _create_principal(session)

        versions = await human_consent.record_consent(
            session,
            subject_principal_id=principal.id,
            settings=Settings(),
            accepted_at=datetime(2026, 6, 27, tzinfo=UTC),
            source_ip_hash="a" * 64,
        )

        rows = (await session.execute(select(HumanConsent))).scalars().all()
    assert {row.document_kind for row in rows} == consents_repo.REQUIRED_DOCUMENT_KINDS
    assert set(versions) == consents_repo.REQUIRED_DOCUMENT_KINDS
    for row in rows:
        document = _current_document(row.document_kind)
        assert row.document_version == document.version_label
        assert row.content_hash == document.content_hash
        assert versions[row.document_kind] == document.version_label


@pytest.mark.asyncio
async def test_document_body_change_invalidates_stored_consent(
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
        assert await human_consent.has_current_consent(
            session,
            subject_principal_id=principal.id,
            settings=Settings(),
        )

        changed_documents = tuple(
            LegalDocument(
                kind=document.kind,
                version_label=document.version_label,
                body=f"{document.body}\nMaterial terms changed.\n",
            )
            if document.kind == consents_repo.DOCUMENT_KIND_TOS
            else document
            for document in list_legal_documents()
        )
        monkeypatch.setattr(human_consent, "list_legal_documents", lambda: changed_documents)

        assert not await human_consent.has_current_consent(
            session,
            subject_principal_id=principal.id,
            settings=Settings(),
        )


@pytest.mark.asyncio
async def test_null_content_hash_consent_rows_are_stale_and_not_backfilled(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        principal = await _create_principal(session)
        accepted_at = datetime(2026, 6, 27, tzinfo=UTC)
        session.add_all(
            [
                HumanConsent(
                    subject_principal_id=principal.id,
                    document_kind=kind,
                    document_version=_current_document(kind).version_label,
                    content_hash=None,
                    accepted_at=accepted_at,
                )
                for kind in sorted(consents_repo.REQUIRED_DOCUMENT_KINDS)
            ]
        )
        await session.flush()

        assert not await human_consent.has_current_consent(
            session,
            subject_principal_id=principal.id,
            settings=Settings(),
        )

        await human_consent.record_consent(
            session,
            subject_principal_id=principal.id,
            settings=Settings(),
            accepted_at=accepted_at,
        )
        rows = (await session.execute(select(HumanConsent))).scalars().all()
        assert len(rows) == 6
        assert sum(row.content_hash is None for row in rows) == 3
        assert sum(row.content_hash is not None for row in rows) == 3
        assert await human_consent.has_current_consent(
            session,
            subject_principal_id=principal.id,
            settings=Settings(),
        )
