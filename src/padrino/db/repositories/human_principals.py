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
from collections.abc import Iterable
from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.core.gdpr_delete import anonymized_principal_token
from padrino.core.ranked_handle_policy import RankedHandleReason, validate_ranked_handle
from padrino.db.models import HumanSession, Principal
from padrino.db.repositories.provider_credentials import revoke_and_erase_for_principal

PRINCIPAL_KIND_GUEST: Final[str] = "guest"
PRINCIPAL_KIND_ACCOUNT: Final[str] = "account"

SESSION_KIND_GUEST: Final[str] = "guest"
SESSION_KIND_ACCOUNT: Final[str] = "account"


class RankedHandleValidationError(ValueError):
    """A requested ranked handle failed the pure name policy."""

    status_code: int = 400

    def __init__(self, reason: RankedHandleReason) -> None:
        self.reason = reason
        super().__init__(reason.value)


class RankedHandleCollisionError(ValueError):
    """A ranked handle normalized to a value already claimed by another user."""

    status_code: int = 409

    def __init__(self, normalized_handle: str) -> None:
        self.normalized_handle = normalized_handle
        super().__init__("ranked_handle_collision")


def hash_session_token(raw: str) -> str:
    """Return the sha256 hex digest stored in ``human_sessions.session_hash``."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def derive_default_ranked_handle(principal_id: uuid.UUID) -> str:
    """Return the deterministic pseudonymous handle for a principal id."""

    digest = hashlib.sha256(f"ranked-handle:{principal_id}".encode()).hexdigest()
    return f"Player-{digest[:10]}"


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


async def allocate_anonymized_principal_token(
    session: AsyncSession,
    principal_id: uuid.UUID,
) -> str:
    """Return the first stable tombstone UUID not used by a principal row."""
    for collision_index in range(1024):
        token = anonymized_principal_token(principal_id, collision_index=collision_index)
        if token == str(principal_id):
            continue
        if await session.get(Principal, uuid.UUID(token)) is None:
            return token
    raise RuntimeError("unable to allocate non-colliding anonymized principal token")


async def anonymize_principal_for_delete(
    session: AsyncSession,
    principal_id: uuid.UUID,
    *,
    now: datetime,
) -> Principal | None:
    """Soft-delete a principal and scrub display/ranked handle fields."""
    obj = await session.get(Principal, principal_id)
    if obj is None:
        return None
    if obj.deleted_at is None:
        obj.deleted_at = now
    obj.display_name = None
    obj.ranked_handle = None
    obj.ranked_handle_normalized = None
    obj.updated_at = now
    await revoke_and_erase_for_principal(
        session,
        principal_id=principal_id,
        revoked_at=now,
    )
    await session.flush()
    return obj


async def claim_ranked_handle(
    session: AsyncSession,
    principal_id: uuid.UUID,
    *,
    candidate: str | None,
    blocklist: Iterable[str],
    reserved: Iterable[str],
    now: datetime,
) -> Principal | None:
    """Validate and atomically claim a ranked handle for ``principal_id``.

    ``candidate=None`` applies the deterministic default handle. The unique
    normalized shadow column is the atomic arbiter; a uniqueness violation is
    surfaced as a 409-shaped exception for API callers.
    """

    principal = await session.get(Principal, principal_id)
    if principal is None:
        return None

    handle_candidate = (
        candidate if candidate is not None else derive_default_ranked_handle(principal_id)
    )
    decision = validate_ranked_handle(
        handle_candidate,
        blocklist=blocklist,
        reserved=reserved,
    )
    if not decision.ok:
        raise RankedHandleValidationError(decision.reason)
    if (
        decision.handle is None or decision.normalized is None
    ):  # pragma: no cover - ok implies both.
        raise RuntimeError("valid ranked handle missing normalized form")

    try:
        async with session.begin_nested():
            principal.ranked_handle = decision.handle
            principal.ranked_handle_normalized = decision.normalized
            principal.updated_at = now
            await session.flush()
    except IntegrityError as exc:
        raise RankedHandleCollisionError(decision.normalized) from exc
    return principal


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


async def list_sessions_for_principal(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
) -> list[HumanSession]:
    """Return all browser sessions for one principal in stable order."""
    stmt = (
        select(HumanSession)
        .where(HumanSession.principal_id == principal_id)
        .order_by(HumanSession.issued_at, HumanSession.id)
    )
    return list((await session.execute(stmt)).scalars())


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
    "RankedHandleCollisionError",
    "RankedHandleValidationError",
    "allocate_anonymized_principal_token",
    "anonymize_principal_for_delete",
    "claim_ranked_handle",
    "create_principal",
    "create_session",
    "derive_default_ranked_handle",
    "get_principal",
    "get_session_by_token",
    "hash_session_token",
    "list_sessions_for_principal",
    "mark_session_seen",
    "revoke_session",
    "set_display_name",
]
