"""One-tap consent + ranked consent enforcement (US-130, US-304).

A human must accept Terms (``TOS``), Privacy (``PRIVACY``), and confirm they are
16+ (``AGE_GATE``) before sending any action or chat. This module is the impure
api/runner shell that:

* resolves the CURRENT required document versions from :class:`Settings`
  (:func:`required_consent_requirements`), so a body/hash change re-prompts
  every human;
* records a one-tap combined consent (:func:`record_consent`), hashing the
  client IP rather than storing it raw; and
* enforces the gate (:func:`enforce_consent` / :func:`require_consent_for`),
  rejecting the first action or chat with HTTP 412 when a current consent is
  missing for any required document kind; and
* records/enforces purpose-segmented ranked consent for the ranked admission
  shell without opening the lobby ranked flag path.

The gate is NEVER enforced in the pure core — only here and in the runner.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.repositories import human_consents as consents_repo
from padrino.legal.documents import DOCUMENT_KIND_RANKED, list_legal_documents
from padrino.settings import Settings

CONSENT_REQUIRED_DETAIL = "consent_required"
RANKED_CONSENT_REQUIRED_DETAIL = "ranked_consent_required"


@dataclass(frozen=True, slots=True)
class ConsentRequirement:
    """The server-authoritative version label and content hash for one document."""

    version_label: str
    content_hash: str


def required_consent_requirements(settings: Settings) -> dict[str, ConsentRequirement]:
    """Map each required document kind to the CURRENT registry hash.

    ``settings`` is retained for call-site compatibility; the legal document
    registry is now the authority for version labels and content hashes.
    """
    del settings
    documents = {document.kind: document for document in list_legal_documents()}
    missing = consents_repo.REQUIRED_DOCUMENT_KINDS - set(documents)
    if missing:
        raise RuntimeError(f"missing required legal documents: {sorted(missing)}")
    return {
        kind: ConsentRequirement(
            version_label=documents[kind].version_label,
            content_hash=documents[kind].content_hash,
        )
        for kind in sorted(consents_repo.REQUIRED_DOCUMENT_KINDS)
    }


def required_consent_versions(settings: Settings) -> dict[str, str]:
    """Map each required document kind to its CURRENT registry version label."""
    requirements = required_consent_requirements(settings)
    return {kind: requirement.version_label for kind, requirement in requirements.items()}


def required_ranked_consent_requirements(settings: Settings) -> dict[str, ConsentRequirement]:
    """Map each ranked purpose kind to the current RANKED registry hash.

    ``settings`` is retained for call-site compatibility. The single RANKED
    document is the server authority; each ranked purpose stores its own consent
    row bound to that document's current version label and content hash.
    """
    del settings
    documents = {document.kind: document for document in list_legal_documents()}
    document = documents.get(DOCUMENT_KIND_RANKED)
    if document is None:
        raise RuntimeError("missing required legal document: RANKED")
    requirement = ConsentRequirement(
        version_label=document.version_label,
        content_hash=document.content_hash,
    )
    return dict.fromkeys(sorted(consents_repo.REQUIRED_RANKED_DOCUMENT_KINDS), requirement)


def required_ranked_consent_versions(settings: Settings) -> dict[str, str]:
    """Map each ranked purpose kind to the CURRENT RANKED version label."""
    requirements = required_ranked_consent_requirements(settings)
    return {kind: requirement.version_label for kind, requirement in requirements.items()}


def hash_source_ip(ip: str | None) -> str | None:
    """Return a sha256 of the client IP (never the raw IP), or None."""
    if ip is None:
        return None
    ip = ip.strip()
    if not ip:
        return None
    return hashlib.sha256(ip.encode("utf-8")).hexdigest()


def client_ip_hash(request: Request) -> str | None:
    """Best-effort hashed client IP for an incoming request."""
    client = request.client
    return hash_source_ip(client.host if client is not None else None)


async def record_consent(
    session: AsyncSession,
    *,
    subject_principal_id: uuid.UUID,
    settings: Settings,
    accepted_at: datetime,
    source_ip_hash: str | None = None,
) -> dict[str, str]:
    """Append a combined consent for every required kind at the current versions.

    Returns the accepted ``{document_kind: document_version}`` map.
    """
    requirements = required_consent_requirements(settings)
    versions = {kind: requirement.version_label for kind, requirement in requirements.items()}
    content_hashes = {kind: requirement.content_hash for kind, requirement in requirements.items()}
    await consents_repo.record_combined_consent(
        session,
        subject_principal_id=subject_principal_id,
        versions=versions,
        content_hashes=content_hashes,
        accepted_at=accepted_at,
        source_ip_hash=source_ip_hash,
    )
    return versions


async def record_ranked_consent(
    session: AsyncSession,
    *,
    subject_principal_id: uuid.UUID,
    settings: Settings,
    accepted_at: datetime,
    source_ip_hash: str | None = None,
) -> dict[str, str]:
    """Append ranked purpose consent rows at the current RANKED document hash."""
    requirements = required_ranked_consent_requirements(settings)
    versions = {kind: requirement.version_label for kind, requirement in requirements.items()}
    content_hashes = {kind: requirement.content_hash for kind, requirement in requirements.items()}
    await consents_repo.record_consent_kinds(
        session,
        subject_principal_id=subject_principal_id,
        kinds=consents_repo.REQUIRED_RANKED_DOCUMENT_KINDS,
        versions=versions,
        content_hashes=content_hashes,
        accepted_at=accepted_at,
        source_ip_hash=source_ip_hash,
    )
    return versions


async def has_current_consent(
    session: AsyncSession,
    *,
    subject_principal_id: uuid.UUID,
    settings: Settings,
) -> bool:
    """True when the principal holds a current consent for every required kind."""
    return await consents_repo.has_current_consent(
        session,
        subject_principal_id=subject_principal_id,
        required_content_hashes={
            kind: requirement.content_hash
            for kind, requirement in required_consent_requirements(settings).items()
        },
    )


async def has_current_ranked_consent(
    session: AsyncSession,
    *,
    subject_principal_id: uuid.UUID,
    settings: Settings,
) -> bool:
    """True when base consent and every ranked purpose consent are current."""
    if not await has_current_consent(
        session,
        subject_principal_id=subject_principal_id,
        settings=settings,
    ):
        return False
    return await consents_repo.has_current_consent_for_kinds(
        session,
        subject_principal_id=subject_principal_id,
        required_kinds=consents_repo.REQUIRED_RANKED_DOCUMENT_KINDS,
        required_content_hashes={
            kind: requirement.content_hash
            for kind, requirement in required_ranked_consent_requirements(settings).items()
        },
    )


async def enforce_consent(
    session: AsyncSession,
    *,
    subject_principal_id: uuid.UUID,
    settings: Settings,
) -> None:
    """Raise HTTP 412 unless the principal holds a current, complete consent.

    Called by the action/chat channels (api/runner) before any human action or
    chat is accepted. A document version bump invalidates a stale consent, so the
    human is re-prompted on their next action.
    """
    if await has_current_consent(
        session, subject_principal_id=subject_principal_id, settings=settings
    ):
        return
    raise HTTPException(
        status_code=status.HTTP_412_PRECONDITION_FAILED,
        detail=CONSENT_REQUIRED_DETAIL,
    )


async def enforce_ranked_consent(
    session: AsyncSession,
    *,
    subject_principal_id: uuid.UUID,
    settings: Settings,
) -> None:
    """Raise HTTP 412 unless base and ranked-purpose consent are current."""
    if await has_current_ranked_consent(
        session, subject_principal_id=subject_principal_id, settings=settings
    ):
        return
    raise HTTPException(
        status_code=status.HTTP_412_PRECONDITION_FAILED,
        detail=RANKED_CONSENT_REQUIRED_DETAIL,
    )


__all__ = [
    "CONSENT_REQUIRED_DETAIL",
    "RANKED_CONSENT_REQUIRED_DETAIL",
    "client_ip_hash",
    "enforce_consent",
    "enforce_ranked_consent",
    "has_current_consent",
    "has_current_ranked_consent",
    "hash_source_ip",
    "record_consent",
    "record_ranked_consent",
    "required_consent_requirements",
    "required_consent_versions",
    "required_ranked_consent_requirements",
    "required_ranked_consent_versions",
]
