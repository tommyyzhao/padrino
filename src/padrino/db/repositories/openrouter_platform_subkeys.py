"""Persistence helpers for optional account-scoped OpenRouter subkeys (US-359)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.db.models import OpenRouterPlatformSubkey


async def get_active(
    session: AsyncSession,
    *,
    principal_id: uuid.UUID,
    lock: bool = False,
) -> OpenRouterPlatformSubkey | None:
    """Return one locally usable account subkey, optionally locked for rotation."""
    statement = select(OpenRouterPlatformSubkey).where(
        OpenRouterPlatformSubkey.principal_id == principal_id,
        OpenRouterPlatformSubkey.status == "ACTIVE",
    )
    if lock:
        statement = statement.with_for_update()
    return cast(OpenRouterPlatformSubkey | None, await session.scalar(statement))


def deactivate(
    row: OpenRouterPlatformSubkey,
    *,
    status: str,
    now: datetime,
) -> None:
    """Make a subkey locally unusable and erase its envelope in this transaction."""
    if status not in {"DISABLED", "REVOKED"}:
        raise ValueError("subkey_status_invalid")
    row.status = status
    row.ciphertext = None
    row.nonce = None
    row.disabled_at = now
    if status == "REVOKED":
        row.revoked_at = now


__all__ = ["deactivate", "get_active"]
