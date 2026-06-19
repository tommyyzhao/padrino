"""Append-only consent records for the one-tap consent + 16+ age gate (US-130).

A human must accept Terms (``TOS``), Privacy (``PRIVACY``), and confirm they are
16+ (``AGE_GATE``) before sending any action or chat. One combined "tap" records
all three kinds at their current versions via :func:`record_combined_consent`.
Rows are NEVER updated in place — a re-acceptance (e.g. after a document version
bump that re-prompts) appends fresh rows, preserving a complete audit trail.

:func:`current_consent_kinds` returns the set of document kinds for which the
principal holds a consent matching the supplied *required versions* map; the
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

REQUIRED_DOCUMENT_KINDS: Final[frozenset[str]] = frozenset(
    {DOCUMENT_KIND_TOS, DOCUMENT_KIND_PRIVACY, DOCUMENT_KIND_AGE_GATE}
)


async def record_combined_consent(
    session: AsyncSession,
    *,
    subject_principal_id: uuid.UUID,
    versions: Mapping[str, str],
    accepted_at: datetime,
    source_ip_hash: str | None = None,
) -> list[HumanConsent]:
    """Append one consent row per required document kind (the one-tap action).

    ``versions`` maps each :data:`REQUIRED_DOCUMENT_KINDS` member to the document
    version being accepted. Returns the freshly-created rows.
    """
    rows = [
        HumanConsent(
            subject_principal_id=subject_principal_id,
            document_kind=kind,
            document_version=versions[kind],
            accepted_at=accepted_at,
            source_ip_hash=source_ip_hash,
        )
        for kind in sorted(REQUIRED_DOCUMENT_KINDS)
    ]
    session.add_all(rows)
    await session.flush()
    return rows


async def current_consent_kinds(
    session: AsyncSession,
    *,
    subject_principal_id: uuid.UUID,
    required_versions: Mapping[str, str],
) -> set[str]:
    """Return the document kinds the principal currently consents to.

    A kind counts as current only when the principal has at least one consent row
    whose ``document_version`` equals ``required_versions[kind]``, so a version
    bump silently drops that kind from the set until the human re-accepts.
    """
    stmt = select(HumanConsent.document_kind, HumanConsent.document_version).where(
        HumanConsent.subject_principal_id == subject_principal_id
    )
    result = await session.execute(stmt)
    current: set[str] = set()
    for kind, version in result.all():
        if required_versions.get(kind) == version:
            current.add(kind)
    return current


async def has_current_consent(
    session: AsyncSession,
    *,
    subject_principal_id: uuid.UUID,
    required_versions: Mapping[str, str],
) -> bool:
    """True when the principal holds a current consent for EVERY required kind."""
    kinds = await current_consent_kinds(
        session,
        subject_principal_id=subject_principal_id,
        required_versions=required_versions,
    )
    return kinds >= REQUIRED_DOCUMENT_KINDS


__all__ = [
    "DOCUMENT_KIND_AGE_GATE",
    "DOCUMENT_KIND_PRIVACY",
    "DOCUMENT_KIND_TOS",
    "REQUIRED_DOCUMENT_KINDS",
    "current_consent_kinds",
    "has_current_consent",
    "record_combined_consent",
]
