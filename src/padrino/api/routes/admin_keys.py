"""Admin key CRUD routes (US-056).

``POST /admin/keys`` is the *only* place a raw key is ever returned — the
response includes the freshly generated ``raw_key`` exactly once. Every
``GET`` exposes the display prefix plus metadata so admins can audit which
keys exist and disable stale ones.
"""

from __future__ import annotations

import base64
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from padrino.api.auth import (
    VALID_SCOPES,
    generate_raw_key,
    require_admin,
)
from padrino.api.deps import get_session
from padrino.api.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MIN_LIMIT,
    CursorPage,
    paginate_keyset,
)
from padrino.db.models import ApiKey
from padrino.db.repositories import api_keys as api_keys_repo

router = APIRouter(prefix="/admin")


def _is_valid_ed25519_pubkey_b64(value: str) -> bool:
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii"))
    except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
        return False
    return len(raw) == 32


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdminKeyCreate(_StrictModel):
    label: str = Field(min_length=1)
    scopes: list[str] = Field(min_length=1)
    submission_public_key: str | None = None


class AdminKeyResponse(BaseModel):
    id: uuid.UUID
    label: str
    scopes: list[str]
    key_prefix: str
    submission_public_key: str | None
    created_at: datetime
    last_used_at: datetime | None
    disabled_at: datetime | None


class AdminKeyCreateResponse(AdminKeyResponse):
    raw_key: str


class AdminKeyListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=DEFAULT_LIMIT, ge=MIN_LIMIT, le=MAX_LIMIT)
    cursor: str | None = None


def _to_response(obj: ApiKey) -> AdminKeyResponse:
    return AdminKeyResponse(
        id=obj.id,
        label=obj.label,
        scopes=list(obj.scopes),
        key_prefix=obj.key_prefix,
        submission_public_key=obj.submission_public_key,
        created_at=obj.created_at,
        last_used_at=obj.last_used_at,
        disabled_at=obj.disabled_at,
    )


@router.post(
    "/keys",
    response_model=AdminKeyCreateResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def create_api_key(
    body: AdminKeyCreate,
    session: AsyncSession = Depends(get_session),
) -> AdminKeyCreateResponse:
    unknown = set(body.scopes) - VALID_SCOPES
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown scope(s): {sorted(unknown)}",
        )
    if body.submission_public_key is not None and not _is_valid_ed25519_pubkey_b64(
        body.submission_public_key
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="submission_public_key must be urlsafe-base64 of a 32-byte Ed25519 key",
        )
    raw_key = generate_raw_key()
    obj = await api_keys_repo.create(
        session,
        raw_key=raw_key,
        scopes=body.scopes,
        label=body.label,
        submission_public_key=body.submission_public_key,
    )
    return AdminKeyCreateResponse(
        id=obj.id,
        label=obj.label,
        scopes=list(obj.scopes),
        key_prefix=obj.key_prefix,
        submission_public_key=obj.submission_public_key,
        created_at=obj.created_at,
        last_used_at=obj.last_used_at,
        disabled_at=obj.disabled_at,
        raw_key=raw_key,
    )


@router.get(
    "/keys",
    response_model=CursorPage[AdminKeyResponse],
    dependencies=[Depends(require_admin)],
)
async def list_api_keys(
    query: Annotated[AdminKeyListQuery, Query()],
    session: AsyncSession = Depends(get_session),
) -> CursorPage[AdminKeyResponse]:
    from sqlalchemy import select

    stmt = select(ApiKey)
    rows, next_cursor = await paginate_keyset(
        session,
        stmt,
        created_at_col=ApiKey.created_at,
        id_col=ApiKey.id,
        limit=query.limit,
        cursor=query.cursor,
    )
    items = [_to_response(r) for r in rows]
    return CursorPage[AdminKeyResponse](items=items, next_cursor=next_cursor)


@router.delete(
    "/keys/{key_id}",
    response_model=AdminKeyResponse,
    dependencies=[Depends(require_admin)],
)
async def disable_api_key(
    key_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> AdminKeyResponse:
    obj = await api_keys_repo.disable(session, key_id)
    if obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"api_key {key_id} not found",
        )
    return _to_response(obj)


__all__ = [
    "AdminKeyCreate",
    "AdminKeyCreateResponse",
    "AdminKeyResponse",
    "router",
]
