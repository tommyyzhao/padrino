"""Guest quickplay + human self-profile routes (US-128).

``POST /human/guest`` mints a guest *principal* and an opaque session token,
persists only the token's sha256 (constant-time compared on lookup), and sets an
http-only + ``SameSite=Lax`` cookie holding the plaintext token. It never touches
``api_keys`` and grants ZERO API scope — a guest cookie is a human identity only.
The endpoint is reachable even when ``create_app(auth_required=True)`` because it
carries no API-scope dependency (the human auth path is fully separate from the
API-key path, US-127).

``PATCH /human/me`` sets a per-session display name (validated, not globally
unique); ``GET /human/me`` returns the current principal. Both require a valid
human session via :func:`padrino.api.human_auth.require_human`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.api.auth import _get_auth_settings
from padrino.api.deps import get_session
from padrino.api.human_auth import (
    HUMAN_SESSION_COOKIE,
    HumanPrincipalContext,
    generate_session_token,
    require_human,
)
from padrino.db.repositories import human_principals as principals_repo

router = APIRouter()


class GuestSummary(BaseModel):
    """Public summary of a guest/account human principal (no PII beyond name)."""

    principal_id: uuid.UUID
    kind: str
    display_name: str | None


class DisplayNameUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = Field(min_length=1, max_length=40)

    @field_validator("display_name")
    @classmethod
    def _strip_non_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("display_name must not be blank")
        return stripped


@router.post(
    "/human/guest",
    response_model=GuestSummary,
    status_code=status.HTTP_201_CREATED,
)
async def create_guest(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> GuestSummary:
    """Create a guest principal + session and set the human session cookie."""
    settings = _get_auth_settings(request)
    raw_token = generate_session_token()
    now = datetime.now(UTC)
    expires_at = now + timedelta(hours=settings.padrino_human_session_ttl_hours)

    principal = await principals_repo.create_principal(
        session, kind=principals_repo.PRINCIPAL_KIND_GUEST
    )
    await principals_repo.create_session(
        session,
        principal_id=principal.id,
        raw_token=raw_token,
        kind=principals_repo.SESSION_KIND_GUEST,
        issued_at=now,
        expires_at=expires_at,
    )

    response.set_cookie(
        key=HUMAN_SESSION_COOKIE,
        value=raw_token,
        max_age=settings.padrino_human_session_ttl_hours * 3600,
        httponly=True,
        secure=settings.padrino_human_session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return GuestSummary(
        principal_id=principal.id,
        kind=principal.kind,
        display_name=principal.display_name,
    )


@router.get("/human/me", response_model=GuestSummary)
async def get_me(
    ctx: HumanPrincipalContext = Depends(require_human),
) -> GuestSummary:
    """Return the current human principal summary."""
    return GuestSummary(
        principal_id=ctx.principal_id,
        kind=ctx.kind,
        display_name=ctx.display_name,
    )


@router.patch("/human/me", response_model=GuestSummary)
async def patch_me(
    body: DisplayNameUpdate,
    ctx: HumanPrincipalContext = Depends(require_human),
    session: AsyncSession = Depends(get_session),
) -> GuestSummary:
    """Set the current principal's display name (validated, not unique)."""
    updated = await principals_repo.set_display_name(
        session,
        ctx.principal_id,
        display_name=body.display_name,
        now=datetime.now(UTC),
    )
    assert updated is not None  # require_human guarantees the principal exists
    return GuestSummary(
        principal_id=updated.id,
        kind=updated.kind,
        display_name=updated.display_name,
    )


__all__ = ["DisplayNameUpdate", "GuestSummary", "router"]
