"""Find-or-create helpers for OAuth account principals (US-129).

A successful OAuth code exchange yields a stable ``(provider, subject)`` pair.
:func:`find_or_create_account` resolves the matching account
:class:`~padrino.db.models.Principal` if one exists, or creates a fresh account
principal + ``oauth_identities`` row otherwise. When the caller is already a
guest, :func:`find_or_create_account` upgrades that single guest *in place*:
the guest principal's sessions re-point to the account and the guest principal is
soft-deleted. There is NO multi-guest merge and NO friends graph in v1.

This module never imports ``secrets`` / ``time`` (the repository-purity guard);
provider tokens are never persisted here — only the stable ``subject``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import HumanSession, OAuthIdentity, Principal
from padrino.db.repositories.human_principals import PRINCIPAL_KIND_ACCOUNT


async def get_account_by_identity(
    session: AsyncSession, *, provider: str, subject: str
) -> Principal | None:
    """Return the account principal linked to ``(provider, subject)``, or None."""
    row = await _get_identity_and_account(session, provider=provider, subject=subject)
    if row is None:
        return None
    _identity, account = row
    return account


async def _get_identity_and_account(
    session: AsyncSession, *, provider: str, subject: str
) -> tuple[OAuthIdentity, Principal] | None:
    stmt = (
        select(OAuthIdentity, Principal)
        .join(Principal, OAuthIdentity.principal_id == Principal.id)
        .where(OAuthIdentity.provider == provider, OAuthIdentity.subject == subject)
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return None
    identity, account = row
    return identity, account


async def find_or_create_account(
    session: AsyncSession,
    *,
    provider: str,
    subject: str,
    display_name: str | None,
    email: str | None,
    email_verified: bool,
    now: datetime,
    upgrade_guest_id: uuid.UUID | None = None,
) -> Principal:
    """Resolve the account for ``(provider, subject)``, creating it if needed.

    When ``upgrade_guest_id`` is supplied AND no account yet exists for this
    identity, the guest principal is upgraded in place: its kind flips to
    ``account``, the ``oauth_identities`` row is attached to it, and its sessions
    keep pointing at it (so the in-flight session/cookie survives). When an
    account already exists, the guest's sessions are re-pointed to that account
    and the guest principal is soft-deleted.
    """
    row = await _get_identity_and_account(session, provider=provider, subject=subject)
    if row is not None:
        identity, account = row
        _apply_email_claims(identity, email=email, email_verified=email_verified)
        await session.flush()
        if upgrade_guest_id is not None and upgrade_guest_id != account.id:
            await _repoint_sessions(session, src=upgrade_guest_id, dst=account.id, now=now)
            await _soft_delete_principal(session, upgrade_guest_id, now=now)
        return account

    if upgrade_guest_id is not None:
        guest = await session.get(Principal, upgrade_guest_id)
        if guest is not None and guest.deleted_at is None:
            guest.kind = PRINCIPAL_KIND_ACCOUNT
            if display_name is not None and guest.display_name is None:
                guest.display_name = display_name
            guest.updated_at = now
            session.add(
                OAuthIdentity(
                    provider=provider,
                    subject=subject,
                    email=_normalize_email(email),
                    email_verified=email_verified,
                    principal_id=guest.id,
                )
            )
            await session.flush()
            return guest

    account = Principal(kind=PRINCIPAL_KIND_ACCOUNT, display_name=display_name)
    session.add(account)
    await session.flush()
    session.add(
        OAuthIdentity(
            provider=provider,
            subject=subject,
            email=_normalize_email(email),
            email_verified=email_verified,
            principal_id=account.id,
        )
    )
    await session.flush()
    return account


def _normalize_email(email: str | None) -> str | None:
    if email is None:
        return None
    normalized = email.strip().lower()
    return normalized or None


def _apply_email_claims(
    identity: OAuthIdentity, *, email: str | None, email_verified: bool
) -> None:
    identity.email = _normalize_email(email)
    identity.email_verified = email_verified


async def _repoint_sessions(
    session: AsyncSession, *, src: uuid.UUID, dst: uuid.UUID, now: datetime
) -> None:
    """Re-point every session of principal ``src`` to principal ``dst``."""
    await session.execute(
        update(HumanSession)
        .where(HumanSession.principal_id == src)
        .values(principal_id=dst, last_seen_at=now)
    )
    await session.flush()


async def _soft_delete_principal(
    session: AsyncSession, principal_id: uuid.UUID, *, now: datetime
) -> None:
    obj = await session.get(Principal, principal_id)
    if obj is None or obj.deleted_at is not None:
        return
    obj.deleted_at = now
    obj.updated_at = now
    await session.flush()


__all__ = [
    "find_or_create_account",
    "get_account_by_identity",
]
