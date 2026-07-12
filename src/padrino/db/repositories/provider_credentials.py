"""Lifecycle persistence for encrypted provider credentials (US-353).

The encryption service introduced in US-354 supplies envelope bytes.  This
repository intentionally sees no plaintext credential or raw PKCE verifier;
it provides only the atomic lifecycle writes needed to revoke/erase a
connection and claim a signed OpenRouter callback exactly once.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import OpenRouterPkceFlow, ProviderCredential


async def revoke_and_erase_for_principal(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    revoked_at: datetime,
) -> int:
    """Revoke all a principal's credentials and clear every envelope byte.

    Idempotent erasure retains only safe connection metadata, preserving a
    privacy-safe lifecycle record while making decryption impossible.  It
    deliberately touches no game, event, replay, or LLM-call table.
    """
    result = await session.execute(
        update(ProviderCredential)
        .where(ProviderCredential.principal_id == principal_id)
        .values(
            ciphertext=None,
            nonce=None,
            revoked_at=func.coalesce(ProviderCredential.revoked_at, revoked_at),
            updated_at=revoked_at,
        )
    )
    await session.flush()
    assert isinstance(result, CursorResult)
    return result.rowcount or 0


async def revoke_and_erase_provider_credential(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    provider: str,
    revoked_at: datetime,
) -> int:
    """Revoke one provider connection while preserving other future providers.

    Local erasure is deliberately idempotent.  A connection route may call it
    after an optional upstream revocation attempt, but upstream failure must
    never retain a locally usable plaintext envelope.
    """
    result = await session.execute(
        update(ProviderCredential)
        .where(
            ProviderCredential.principal_id == principal_id,
            ProviderCredential.provider == provider,
            ProviderCredential.revoked_at.is_(None),
        )
        .values(
            ciphertext=None,
            nonce=None,
            revoked_at=revoked_at,
            updated_at=revoked_at,
        )
    )
    await session.flush()
    assert isinstance(result, CursorResult)
    return result.rowcount or 0


async def try_consume_openrouter_pkce_flow(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    state_hash: str,
    verifier_binding_hash: str,
    consumed_at: datetime,
) -> bool:
    """Atomically consume one unexpired callback state exactly once.

    The conditional update also binds the callback's derived verifier hash to
    the persisted flow. Racing callbacks can both issue the statement, but
    only one observes a matching, unconsumed, unexpired row and changes it.
    Callers must reject ``False`` before exchanging an authorization code.
    """
    result = await session.execute(
        update(OpenRouterPkceFlow)
        .where(
            OpenRouterPkceFlow.principal_id == principal_id,
            OpenRouterPkceFlow.state_hash == state_hash,
            OpenRouterPkceFlow.verifier_binding_hash == verifier_binding_hash,
            OpenRouterPkceFlow.consumed_at.is_(None),
            OpenRouterPkceFlow.expires_at > consumed_at,
        )
        .values(consumed_at=consumed_at)
    )
    await session.flush()
    assert isinstance(result, CursorResult)
    return bool(result.rowcount)


__all__ = [
    "revoke_and_erase_for_principal",
    "revoke_and_erase_provider_credential",
    "try_consume_openrouter_pkce_flow",
]
