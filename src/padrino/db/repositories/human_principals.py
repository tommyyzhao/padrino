"""CRUD helpers for browser-human principals and sessions (US-127).

Raw session tokens are NEVER persisted: the caller mints a token (token minting
lives in the impure :mod:`padrino.api.human_auth` shell, like API-key generation
lives in :mod:`padrino.api.auth`, so this repository module stays free of
``secrets`` / ``time``), the digest is stored in ``human_sessions.session_hash``
via :func:`hash_session_token`, and lookups compare on the digest. This module is
completely independent of :mod:`padrino.db.repositories.api_keys` — a
guest/account session never touches ``api_keys`` and vice-versa.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import HumanSession, Principal

PRINCIPAL_KIND_GUEST: Final[str] = "guest"
PRINCIPAL_KIND_ACCOUNT: Final[str] = "account"

SESSION_KIND_GUEST: Final[str] = "guest"
SESSION_KIND_ACCOUNT: Final[str] = "account"


def hash_session_token(raw: str) -> str:
    """Return the sha256 hex digest stored in ``human_sessions.session_hash``."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def create_principal(
    session: AsyncSession,
    *,
    kind: str,
    display_name: str | None = None,
) -> Principal:
    obj = Principal(kind=kind, display_name=display_name)
    session.add(obj)
    await session.flush()
    return obj


async def get_principal(session: AsyncSession, principal_id: uuid.UUID) -> Principal | None:
    return await session.get(Principal, principal_id)


async def set_display_name(
    session: AsyncSession, principal_id: uuid.UUID, *, display_name: str | None, now: datetime
) -> Principal | None:
    obj = await session.get(Principal, principal_id)
    if obj is None:
        return None
    obj.display_name = display_name
    obj.updated_at = now
    await session.flush()
    return obj


async def create_session(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    raw_token: str,
    kind: str,
    issued_at: datetime,
    expires_at: datetime,
) -> HumanSession:
    """Persist a new session for ``principal_id`` (token stored only as sha256)."""
    obj = HumanSession(
        principal_id=principal_id,
        session_hash=hash_session_token(raw_token),
        kind=kind,
        issued_at=issued_at,
        expires_at=expires_at,
    )
    session.add(obj)
    await session.flush()
    return obj


async def get_session_by_token(session: AsyncSession, raw_token: str) -> HumanSession | None:
    """Look up a session by its raw token (hashed for the comparison)."""
    digest = hash_session_token(raw_token)
    stmt = select(HumanSession).where(HumanSession.session_hash == digest)
    return (await session.execute(stmt)).scalar_one_or_none()


async def mark_session_seen(session: AsyncSession, session_id: uuid.UUID, *, now: datetime) -> None:
    obj = await session.get(HumanSession, session_id)
    if obj is None:
        return
    obj.last_seen_at = now
    await session.flush()


async def revoke_session(
    session: AsyncSession, session_id: uuid.UUID, *, now: datetime
) -> HumanSession | None:
    obj = await session.get(HumanSession, session_id)
    if obj is None:
        return None
    if obj.revoked_at is None:
        obj.revoked_at = now
    await session.flush()
    return obj


__all__ = [
    "PRINCIPAL_KIND_ACCOUNT",
    "PRINCIPAL_KIND_GUEST",
    "SESSION_KIND_ACCOUNT",
    "SESSION_KIND_GUEST",
    "create_principal",
    "create_session",
    "get_principal",
    "get_session_by_token",
    "hash_session_token",
    "mark_session_seen",
    "revoke_session",
    "set_display_name",
]
