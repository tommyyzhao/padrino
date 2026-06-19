"""One-tap consent + 16+ age-gate enforcement (US-130).

A human must accept Terms (``TOS``), Privacy (``PRIVACY``), and confirm they are
16+ (``AGE_GATE``) before sending any action or chat. This module is the impure
api/runner shell that:

* resolves the CURRENT required document versions from :class:`Settings`
  (:func:`required_consent_versions`), so a version bump re-prompts every human;
* records a one-tap combined consent (:func:`record_consent`), hashing the
  client IP rather than storing it raw; and
* enforces the gate (:func:`enforce_consent` / :func:`require_consent_for`),
  rejecting the first action or chat with HTTP 412 when a current consent is
  missing for any required document kind.

The gate is NEVER enforced in the pure core — only here and in the runner.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from fastapi import HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.repositories import human_consents as consents_repo
from padrino.settings import Settings

CONSENT_REQUIRED_DETAIL = "consent_required"


def required_consent_versions(settings: Settings) -> dict[str, str]:
    """Map each required document kind to its CURRENT version from settings."""
    return {
        consents_repo.DOCUMENT_KIND_TOS: settings.padrino_consent_tos_version,
        consents_repo.DOCUMENT_KIND_PRIVACY: settings.padrino_consent_privacy_version,
        consents_repo.DOCUMENT_KIND_AGE_GATE: settings.padrino_consent_age_gate_version,
    }


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
    versions = required_consent_versions(settings)
    await consents_repo.record_combined_consent(
        session,
        subject_principal_id=subject_principal_id,
        versions=versions,
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
        required_versions=required_consent_versions(settings),
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


__all__ = [
    "CONSENT_REQUIRED_DETAIL",
    "client_ip_hash",
    "enforce_consent",
    "has_current_consent",
    "hash_source_ip",
    "record_consent",
    "required_consent_versions",
]
