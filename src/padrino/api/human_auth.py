"""Browser-human identity/session auth, separate from API-key auth (US-127).

This module is the human counterpart of :mod:`padrino.api.auth`. It resolves a
human *principal* from an opaque session cookie WITHOUT ever consulting
``api_keys``. The two auth paths are provably non-overlapping:

* :func:`padrino.api.auth.get_auth_context` reads the ``Authorization`` /
  ``X-Padrino-Admin-Token`` headers and grants API *scopes*. It never reads the
  human session cookie, so a guest cookie grants ZERO API scope.
* :func:`get_human_context` reads only the human session cookie and grants a
  human *principal* identity. It never reads a Bearer token, so an API key
  grants ZERO human identity.

A session is valid only when its principal is not soft-deleted, the session is
not revoked, and ``expires_at`` is still in the future. Rate limiting reuses the
shared :class:`padrino.api.auth.RateLimiter`, keyed by the session hash with a
``human:`` namespace so it cannot collide with an api-key bucket.

:func:`require_human` enforces *any* valid human principal (401 otherwise).
:func:`require_account` additionally requires an ``account`` principal (403 for a
guest), so an account-only route rejects a guest with 403, not 401.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.api.auth import RateLimiter, _get_auth_settings, _get_rate_limiter
from padrino.api.deps import get_session
from padrino.db.repositories import human_principals as principals_repo
from padrino.settings import Settings

HUMAN_SESSION_COOKIE: Final[str] = "padrino_human_session"

PRINCIPAL_GUEST: Final[str] = principals_repo.PRINCIPAL_KIND_GUEST
PRINCIPAL_ACCOUNT: Final[str] = principals_repo.PRINCIPAL_KIND_ACCOUNT

_RATE_LIMIT_NAMESPACE: Final[str] = "human:"
_SESSION_TOKEN_BYTES: Final[int] = 32  # 256-bit opaque body


def generate_session_token() -> str:
    """Return a fresh opaque url-safe session token (never persisted raw).

    Token minting lives in the impure API shell (this module), mirroring how
    API-key generation lives in :func:`padrino.api.auth.generate_raw_key`, so the
    repository layer stays free of ``secrets``.
    """
    return secrets.token_urlsafe(_SESSION_TOKEN_BYTES)


@dataclass(frozen=True)
class HumanPrincipalContext:
    """The authenticated browser-human for the current request.

    Carries a principal identity only — never any API scope. ``is_account`` is
    True only for an OAuth-upgraded account principal (US-129).
    """

    principal_id: uuid.UUID
    kind: str
    display_name: str | None
    session_id: uuid.UUID

    @property
    def is_account(self) -> bool:
        return self.kind == PRINCIPAL_ACCOUNT

    @property
    def is_guest(self) -> bool:
        return self.kind == PRINCIPAL_GUEST


def _session_token(request: Request) -> str | None:
    token = request.cookies.get(HUMAN_SESSION_COOKIE)
    if token is None:
        return None
    token = token.strip()
    return token or None


async def get_human_context(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HumanPrincipalContext | None:
    """Resolve the human principal for the current request, or ``None``.

    Returns ``None`` (never raises) when there is no cookie or the session is
    invalid/expired/revoked — the optional resolver. Routes that REQUIRE a human
    layer on top of this via :func:`require_human` / :func:`require_account`.

    This never reads ``api_keys`` or the ``Authorization`` header, so it is
    provably non-overlapping with :func:`padrino.api.auth.get_auth_context`.
    """
    raw_token = _session_token(request)
    if raw_token is None:
        return None

    record = await principals_repo.get_session_by_token(session, raw_token)
    if record is None:
        return None

    now = datetime.now(UTC)
    if record.revoked_at is not None:
        return None
    if _as_aware(record.expires_at) <= now:
        return None

    principal = await principals_repo.get_principal(session, record.principal_id)
    if principal is None or principal.deleted_at is not None:
        return None

    settings = _get_auth_settings(request)
    limiter = _get_rate_limiter(request)
    await _enforce_human_rate_limit(limiter, record.session_hash, settings=settings)

    await principals_repo.mark_session_seen(session, record.id, now=now)
    return HumanPrincipalContext(
        principal_id=principal.id,
        kind=principal.kind,
        display_name=principal.display_name,
        session_id=record.id,
    )


def _as_aware(value: datetime) -> datetime:
    """Coerce a possibly-naive DB timestamp (SQLite drops tz) to UTC-aware."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


async def _enforce_human_rate_limit(
    limiter: RateLimiter,
    session_hash: str,
    *,
    settings: Settings,
) -> None:
    ceiling = settings.padrino_rate_limit_human_per_minute
    if ceiling <= 0:
        return
    bucket_key = f"{_RATE_LIMIT_NAMESPACE}{session_hash}"
    allowed, retry_after = await limiter.hit(bucket_key, limit_per_minute=ceiling)
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate_limited",
            headers={"Retry-After": str(max(1, round(retry_after)))},
        )


async def require_human(
    ctx: HumanPrincipalContext | None = Depends(get_human_context),
) -> HumanPrincipalContext:
    """Require any valid human principal; 401 when no valid session exists."""
    if ctx is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="human_authentication_required",
        )
    return ctx


async def require_account(
    ctx: HumanPrincipalContext = Depends(require_human),
) -> HumanPrincipalContext:
    """Require an account principal; 403 for a guest, 401 for no session."""
    if not ctx.is_account:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="account_required",
        )
    return ctx


__all__ = [
    "HUMAN_SESSION_COOKIE",
    "PRINCIPAL_ACCOUNT",
    "PRINCIPAL_GUEST",
    "HumanPrincipalContext",
    "generate_session_token",
    "get_human_context",
    "require_account",
    "require_human",
]
