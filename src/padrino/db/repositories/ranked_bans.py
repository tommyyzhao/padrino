"""Repository helpers for manual ranked bans and suspensions (US-300)."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.ranked_ban import RankedBanKind, RankedBanSnapshot, is_ranked_banned
from padrino.db.models import OAuthIdentity, RankedBan


def normalize_email_for_ranked_ban(email: str | None) -> str | None:
    """Return the canonical email string used only before hashing."""

    if email is None:
        return None
    normalized = email.strip().lower()
    return normalized or None


def hash_email_for_ranked_ban(email: str | None) -> str | None:
    """Return the sha256 email hash stored on ranked bans, never the raw email."""

    normalized = normalize_email_for_ranked_ban(email)
    if normalized is None:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def issue_ban_for_principal(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID | str,
    kind: RankedBanKind | str,
    reason: str,
    created_by: str,
    now: datetime,
    expires_at: datetime | None = None,
) -> RankedBan:
    """Create a manual ranked ban/suspension for ``principal_id``.

    When the account has a verified OAuth email, the row also stores the email's
    sha256 hash so a later account with the same verified email inherits the
    ban. Accounts without a verified email receive a principal-only ban with
    ``email_hash`` left NULL.
    """

    principal_uuid = _principal_uuid(principal_id)
    kind_value = RankedBanKind(kind).value
    hashes = await _verified_email_hashes(session, principal_uuid)
    obj = RankedBan(
        principal_id=principal_uuid,
        email_hash=min(hashes) if hashes else None,
        kind=kind_value,
        reason=reason,
        created_by=created_by,
        created_at=now,
        expires_at=expires_at,
    )
    session.add(obj)
    await session.flush()
    return obj


async def get(session: AsyncSession, ban_id: uuid.UUID | str) -> RankedBan | None:
    """Return one ranked-ban row by id."""

    return await session.get(RankedBan, _ban_uuid(ban_id))


async def lift_ban(
    session: AsyncSession,
    ban_id: uuid.UUID | str,
    *,
    now: datetime,
) -> RankedBan | None:
    """Mark a ranked-ban row lifted, idempotently."""

    obj = await get(session, ban_id)
    if obj is None:
        return None
    if obj.lifted_at is None:
        obj.lifted_at = now
        await session.flush()
    return obj


async def list_bans_for_principal(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID | str,
) -> list[RankedBan]:
    """Return bans matching the principal id or any verified email hash."""

    principal_uuid = _principal_uuid(principal_id)
    email_hashes = await _verified_email_hashes(session, principal_uuid)
    conditions = [RankedBan.principal_id == principal_uuid]
    if email_hashes:
        conditions.append(RankedBan.email_hash.in_(email_hashes))
    stmt = select(RankedBan).where(or_(*conditions)).order_by(RankedBan.created_at, RankedBan.id)
    return list((await session.execute(stmt)).scalars())


async def is_principal_ranked_banned(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID | str,
    now: datetime,
) -> bool:
    """Resolve DB rows and evaluate the pure ranked-ban predicate."""

    rows = await list_bans_for_principal(session, principal_id=principal_id)
    snapshots = [
        RankedBanSnapshot(kind=row.kind, expires_at=row.expires_at, lifted_at=row.lifted_at)
        for row in rows
    ]
    return is_ranked_banned(snapshots, now)


async def _verified_email_hashes(session: AsyncSession, principal_id: uuid.UUID) -> set[str]:
    stmt = select(OAuthIdentity.email).where(
        OAuthIdentity.principal_id == principal_id,
        OAuthIdentity.email_verified.is_(True),
        OAuthIdentity.email.is_not(None),
    )
    hashes: set[str] = set()
    for email in (await session.execute(stmt)).scalars():
        hashed = hash_email_for_ranked_ban(email)
        if hashed is not None:
            hashes.add(hashed)
    return hashes


def _principal_uuid(value: uuid.UUID | str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


def _ban_uuid(value: uuid.UUID | str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(str(value))


__all__ = [
    "get",
    "hash_email_for_ranked_ban",
    "is_principal_ranked_banned",
    "issue_ban_for_principal",
    "lift_ban",
    "list_bans_for_principal",
    "normalize_email_for_ranked_ban",
]
