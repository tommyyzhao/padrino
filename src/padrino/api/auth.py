"""Scoped API-key authentication with per-key rate limiting (US-056).

This module centralizes the auth model the rest of ``padrino.api.*`` depends
on. Three scopes exist: ``admin`` (full read+write), ``submitter`` (POST
``/ingest`` only — wired in by US-062), and ``spectator`` (read-only). The
``admin`` scope satisfies any required scope.

Authentication is opt-in via ``create_app(auth_required=True)``. When the
flag is off, requests pass through with a synthetic admin context so that
existing test suites and unauthenticated dev environments keep working
unchanged.

Two header schemes are accepted:

* ``Authorization: Bearer pk_...`` — the canonical path. The raw key is
  hashed with sha256 and looked up against ``api_keys.key_hash`` using
  ``hmac.compare_digest`` for constant-time comparison.
* ``X-Padrino-Admin-Token: <token>`` — the US-044 back-compat shim. Routes
  that have this header set the ``Deprecation`` + ``Sunset`` response
  headers via :class:`AdminTokenDeprecationMiddleware`.

Rate limiting uses an in-process sliding-window counter (a deque of timestamps
per key). The window is one minute; per-scope ceilings come from
:class:`padrino.settings.Settings`. The clock is injectable via
``RateLimiter(clock=...)`` so tests can pin time without sleeping.
"""

from __future__ import annotations

import hmac
import secrets
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.api.deps import get_session
from padrino.db.repositories import api_keys as api_keys_repo
from padrino.settings import Settings, get_settings

SCOPE_ADMIN = "admin"
SCOPE_SUBMITTER = "submitter"
SCOPE_SPECTATOR = "spectator"
VALID_SCOPES = frozenset({SCOPE_ADMIN, SCOPE_SUBMITTER, SCOPE_SPECTATOR})

RAW_KEY_PREFIX = "pk_"
_RAW_KEY_TOKEN_BYTES = 32  # 256-bit random body
_DEPRECATION_SUNSET = "Sun, 01 Jan 2027 00:00:00 GMT"


def generate_raw_key() -> str:
    """Return a fresh ``pk_<urlsafe-base64>`` token."""
    body = secrets.token_urlsafe(_RAW_KEY_TOKEN_BYTES)
    return f"{RAW_KEY_PREFIX}{body}"


@dataclass(frozen=True)
class ApiKeyContext:
    """The authenticated principal for the current request."""

    id: uuid.UUID | None
    scopes: frozenset[str]
    via_admin_token: bool = False
    """True when the request authenticated via the deprecated X-Padrino-Admin-Token header."""

    @property
    def synthetic_admin(self) -> bool:
        """True when auth is disabled and the context is fabricated."""
        return self.id is None and not self.via_admin_token

    def has_scope(self, required: set[str]) -> bool:
        if SCOPE_ADMIN in self.scopes:
            return True
        return bool(self.scopes & required)


@dataclass
class RateLimiter:
    """In-process sliding-window rate limiter."""

    clock: Callable[[], float] = time.monotonic
    window_seconds: float = 60.0
    _events: dict[uuid.UUID, deque[float]] = field(default_factory=dict)

    def hit(self, key_id: uuid.UUID, *, limit_per_minute: int) -> tuple[bool, float]:
        """Record one hit. Return ``(allowed, retry_after_seconds)``.

        ``retry_after_seconds`` is ``0.0`` when the request is allowed; it is
        the number of seconds until the oldest event drops out of the window
        when the limit is exhausted.
        """
        now = self.clock()
        window_start = now - self.window_seconds
        bucket = self._events.setdefault(key_id, deque())
        while bucket and bucket[0] <= window_start:
            bucket.popleft()
        if len(bucket) >= limit_per_minute:
            oldest = bucket[0]
            retry_after = max(0.0, oldest + self.window_seconds - now)
            return False, retry_after
        bucket.append(now)
        return True, 0.0

    def reset(self) -> None:
        self._events.clear()


def _limit_for_scopes(scopes: frozenset[str], settings: Settings) -> int:
    """Return the most generous applicable rate-limit ceiling."""
    if SCOPE_ADMIN in scopes:
        return settings.padrino_rate_limit_admin_per_minute
    if SCOPE_SPECTATOR in scopes:
        return settings.padrino_rate_limit_spectator_per_minute
    if SCOPE_SUBMITTER in scopes:
        return settings.padrino_rate_limit_submitter_per_minute
    return 0


def _get_rate_limiter(request: Request) -> RateLimiter:
    limiter: Any = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        limiter = RateLimiter()
        request.app.state.rate_limiter = limiter
    return limiter  # type: ignore[no-any-return]


def _get_auth_settings(request: Request) -> Settings:
    cfg: Any = getattr(request.app.state, "auth_settings", None)
    if cfg is None:
        cfg = get_settings()
    return cfg  # type: ignore[no-any-return]


def _auth_required(request: Request) -> bool:
    return bool(getattr(request.app.state, "auth_required", False))


def _admin_token(request: Request) -> str | None:
    token: Any = getattr(request.app.state, "admin_token", None)
    if token is None:
        return None
    return str(token)


async def get_auth_context(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ApiKeyContext:
    """Resolve the authenticated principal for the current request.

    Always returns a context — never raises 401 here. Scope enforcement
    lives in :func:`require_scopes`, which is layered on top via per-route
    dependencies. This split keeps the rate-limit + auth check at a single
    seam regardless of which scopes a given route requires.
    """
    auth_required = _auth_required(request)
    admin_token = _admin_token(request)
    settings = _get_auth_settings(request)
    limiter = _get_rate_limiter(request)

    # Back-compat shim: X-Padrino-Admin-Token is treated as admin scope when
    # the server has a configured token. The response middleware appends
    # ``Deprecation: true`` + ``Sunset`` headers.
    legacy_header = request.headers.get("X-Padrino-Admin-Token")
    if (
        admin_token is not None
        and legacy_header is not None
        and hmac.compare_digest(legacy_header, admin_token)
    ):
        request.state.admin_token_deprecation = True
        return ApiKeyContext(
            id=None,
            scopes=frozenset({SCOPE_ADMIN}),
            via_admin_token=True,
        )

    bearer = _parse_bearer(request.headers.get("Authorization"))
    if bearer is not None:
        record = await api_keys_repo.get_by_raw(session, bearer)
        if record is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid_api_key",
            )
        if record.disabled_at is not None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="api_key_disabled",
            )
        scopes = frozenset(record.scopes)
        ceiling = _limit_for_scopes(scopes, settings)
        if ceiling <= 0:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="no_valid_scope",
            )
        allowed, retry_after = limiter.hit(record.id, limit_per_minute=ceiling)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="rate_limited",
                headers={"Retry-After": str(max(1, round(retry_after)))},
            )
        await api_keys_repo.mark_used(session, record.id, now=datetime.now(UTC))
        return ApiKeyContext(id=record.id, scopes=scopes, via_admin_token=False)

    if auth_required:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication_required",
        )
    # Synthetic admin context: keeps unauth'd dev / existing tests green.
    return ApiKeyContext(id=None, scopes=frozenset({SCOPE_ADMIN}), via_admin_token=False)


def _parse_bearer(header: str | None) -> str | None:
    if header is None:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def require_scopes(*required: str) -> Callable[..., Awaitable[ApiKeyContext]]:
    """Return a FastAPI dependency that enforces the supplied scope set.

    ``admin`` callers always pass; non-admin callers need at least one
    matching scope in their record. Synthetic admin contexts (auth disabled)
    pass every check.
    """
    required_set = set(required)
    if not required_set or not required_set.issubset(VALID_SCOPES):
        raise ValueError(f"unknown scope(s): {required_set - VALID_SCOPES or required_set}")

    async def _dep(
        ctx: ApiKeyContext = Depends(get_auth_context),
    ) -> ApiKeyContext:
        if not ctx.has_scope(required_set):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="insufficient_scope",
            )
        return ctx

    return _dep


require_admin = require_scopes(SCOPE_ADMIN)
require_read = require_scopes(SCOPE_ADMIN, SCOPE_SPECTATOR)


async def admin_token_deprecation_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Any]],
) -> Any:
    """Stamp ``Deprecation`` / ``Sunset`` headers when X-Padrino-Admin-Token was used."""
    response = await call_next(request)
    if getattr(request.state, "admin_token_deprecation", False):
        response.headers["Deprecation"] = "true"
        response.headers["Sunset"] = _DEPRECATION_SUNSET
    return response


__all__ = [
    "RAW_KEY_PREFIX",
    "SCOPE_ADMIN",
    "SCOPE_SPECTATOR",
    "SCOPE_SUBMITTER",
    "VALID_SCOPES",
    "ApiKeyContext",
    "RateLimiter",
    "admin_token_deprecation_middleware",
    "generate_raw_key",
    "get_auth_context",
    "require_admin",
    "require_read",
    "require_scopes",
]
