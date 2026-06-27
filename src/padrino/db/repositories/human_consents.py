"""Append-only consent records for human legal gates (US-130, US-304).

A human must accept Terms (``TOS``), Privacy (``PRIVACY``), and confirm they are
16+ (``AGE_GATE``) before sending any action or chat. One combined "tap" records
all three kinds at their current versions via :func:`record_combined_consent`.
Rows are NEVER updated in place — a re-acceptance (e.g. after a document version
body/hash change that re-prompts) appends fresh rows, preserving a complete
audit trail.

Ranked play adds purpose-segmented consent kinds. Each purpose is recorded as a
separate row so later withdrawal/GDPR stories can reason about one purpose
without flattening the legal history.

:func:`current_consent_kinds` returns the set of document kinds for which the
principal holds a consent matching the supplied *required content hashes* map; the
api/runner enforcement layer compares it against :data:`REQUIRED_DOCUMENT_KINDS`
to decide whether the principal may act. The enforcement lives in the impure
shell, never in the pure core.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import HumanConsent

DOCUMENT_KIND_TOS: Final[str] = "TOS"
DOCUMENT_KIND_PRIVACY: Final[str] = "PRIVACY"
DOCUMENT_KIND_AGE_GATE: Final[str] = "AGE_GATE"
DOCUMENT_KIND_RANKED: Final[str] = "RANKED"
DOCUMENT_KIND_RANKED_PERSISTENT_RATING: Final[str] = "RANKED:PERSISTENT_RATING"
DOCUMENT_KIND_RANKED_PUBLIC_HANDLE: Final[str] = "RANKED:PUBLIC_HANDLE"
DOCUMENT_KIND_RANKED_CHAT_RETENTION: Final[str] = "RANKED:CHAT_RETENTION"
DOCUMENT_KIND_RANKED_ANTICHEAT_PROFILING: Final[str] = "RANKED:ANTICHEAT_PROFILING"

REQUIRED_DOCUMENT_KINDS: Final[frozenset[str]] = frozenset(
    {DOCUMENT_KIND_TOS, DOCUMENT_KIND_PRIVACY, DOCUMENT_KIND_AGE_GATE}
)
REQUIRED_RANKED_DOCUMENT_KINDS: Final[frozenset[str]] = frozenset(
    {
        DOCUMENT_KIND_RANKED_PERSISTENT_RATING,
        DOCUMENT_KIND_RANKED_PUBLIC_HANDLE,
        DOCUMENT_KIND_RANKED_CHAT_RETENTION,
        DOCUMENT_KIND_RANKED_ANTICHEAT_PROFILING,
    }
)


async def record_combined_consent(
    session: AsyncSession,
    *,
    subject_principal_id: uuid.UUID,
    versions: Mapping[str, str],
    content_hashes: Mapping[str, str],
    accepted_at: datetime,
    source_ip_hash: str | None = None,
) -> list[HumanConsent]:
    """Append one consent row per required document kind (the one-tap action).

    ``versions`` maps each :data:`REQUIRED_DOCUMENT_KINDS` member to the document
    version label being accepted. ``content_hashes`` is the server-authoritative
    hash for each same document kind. Returns the freshly-created rows.
    """
    return await record_consent_kinds(
        session,
        subject_principal_id=subject_principal_id,
        kinds=REQUIRED_DOCUMENT_KINDS,
        versions=versions,
        content_hashes=content_hashes,
        accepted_at=accepted_at,
        source_ip_hash=source_ip_hash,
    )


async def record_consent_kinds(
    session: AsyncSession,
    *,
    subject_principal_id: uuid.UUID,
    kinds: set[str] | frozenset[str],
    versions: Mapping[str, str],
    content_hashes: Mapping[str, str],
    accepted_at: datetime,
    source_ip_hash: str | None = None,
) -> list[HumanConsent]:
    """Append one consent row for each supplied document kind."""
    rows = [
        HumanConsent(
            subject_principal_id=subject_principal_id,
            document_kind=kind,
            document_version=versions[kind],
            content_hash=content_hashes[kind],
            accepted_at=accepted_at,
            source_ip_hash=source_ip_hash,
        )
        for kind in sorted(kinds)
    ]
    session.add_all(rows)
    await session.flush()
    return rows


async def current_consent_kinds(
    session: AsyncSession,
    *,
    subject_principal_id: uuid.UUID,
    required_content_hashes: Mapping[str, str],
) -> set[str]:
    """Return the document kinds the principal currently consents to.

    A kind counts as current only when the principal has at least one consent row
    whose ``content_hash`` equals ``required_content_hashes[kind]``, so a body
    change silently drops that kind from the set until the human re-accepts.
    """
    stmt = select(HumanConsent.document_kind, HumanConsent.content_hash).where(
        HumanConsent.subject_principal_id == subject_principal_id
    )
    result = await session.execute(stmt)
    current: set[str] = set()
    for kind, content_hash in result.all():
        if required_content_hashes.get(kind) == content_hash:
            current.add(kind)
    return current


async def has_current_consent(
    session: AsyncSession,
    *,
    subject_principal_id: uuid.UUID,
    required_content_hashes: Mapping[str, str],
) -> bool:
    """True when the principal holds a current consent for EVERY required kind."""
    return await has_current_consent_for_kinds(
        session,
        subject_principal_id=subject_principal_id,
        required_kinds=REQUIRED_DOCUMENT_KINDS,
        required_content_hashes=required_content_hashes,
    )


async def has_current_consent_for_kinds(
    session: AsyncSession,
    *,
    subject_principal_id: uuid.UUID,
    required_kinds: set[str] | frozenset[str],
    required_content_hashes: Mapping[str, str],
) -> bool:
    """True when the principal holds current consent for every supplied kind."""
    kinds = await current_consent_kinds(
        session,
        subject_principal_id=subject_principal_id,
        required_content_hashes=required_content_hashes,
    )
    return kinds >= required_kinds


__all__ = [
    "DOCUMENT_KIND_AGE_GATE",
    "DOCUMENT_KIND_PRIVACY",
    "DOCUMENT_KIND_RANKED",
    "DOCUMENT_KIND_RANKED_ANTICHEAT_PROFILING",
    "DOCUMENT_KIND_RANKED_CHAT_RETENTION",
    "DOCUMENT_KIND_RANKED_PERSISTENT_RATING",
    "DOCUMENT_KIND_RANKED_PUBLIC_HANDLE",
    "DOCUMENT_KIND_TOS",
    "REQUIRED_DOCUMENT_KINDS",
    "REQUIRED_RANKED_DOCUMENT_KINDS",
    "current_consent_kinds",
    "has_current_consent",
    "has_current_consent_for_kinds",
    "record_combined_consent",
    "record_consent_kinds",
]
