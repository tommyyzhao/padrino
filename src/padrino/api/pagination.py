"""Cursor-based pagination for API list endpoints (US-055).

Every list endpoint accepts ``?limit=<1..200, default 50>&cursor=<opaque>`` and
returns a :class:`CursorPage` whose ``next_cursor`` is a stable opaque token
derived from the last row's ``(created_at, id)`` pair. The cursor survives
schema migrations as long as both columns remain on the source table — they
are read directly from the ORM rows by the route, never persisted, so adding
or dropping unrelated columns has no effect.

Two cursor flavors are provided:

* :func:`encode_cursor` / :func:`decode_cursor` — keyset cursor over
  ``(created_at, id)`` for time-ordered DB lists (games, gauntlets, providers,
  prompts, etc.).
* :func:`encode_index_cursor` / :func:`decode_index_cursor` — opaque offset
  cursor for in-memory lists whose ordering key isn't a DB row pair (the
  computed leaderboard).

Both raise :class:`InvalidCursorError` on tampering; routes turn that into a
400 ``invalid_cursor`` response.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Generic, Protocol, TypeVar, runtime_checkable

from fastapi import HTTPException, status
from pydantic import BaseModel
from sqlalchemy import Select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

T = TypeVar("T")


@runtime_checkable
class _HasCreatedAtAndId(Protocol):
    """Structural type for ORM rows that paginate_keyset can consume."""

    @property
    def created_at(self) -> datetime: ...

    @property
    def id(self) -> uuid.UUID: ...


RowT = TypeVar("RowT", bound=_HasCreatedAtAndId)

DEFAULT_LIMIT = 50
MIN_LIMIT = 1
MAX_LIMIT = 200


class CursorPage(BaseModel, Generic[T]):
    """Paginated response envelope with a stable cursor."""

    items: list[T]
    next_cursor: str | None = None
    total_estimate: int | None = None


class InvalidCursorError(Exception):
    """Raised when a client-supplied cursor cannot be decoded."""


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(s: str) -> bytes:
    padded = s + "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    """Encode ``(created_at, id)`` to an opaque, deployment-stable token."""
    payload = [created_at.isoformat(), str(row_id)]
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return _b64encode(raw)


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """Decode a token from :func:`encode_cursor`.

    Raises :class:`InvalidCursorError` on any failure mode (bad base64, bad
    JSON, wrong shape, unparseable datetime or UUID).
    """
    try:
        raw = _b64decode(cursor)
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise InvalidCursorError(str(exc)) from exc
    if not isinstance(payload, list) or len(payload) != 2:
        raise InvalidCursorError("cursor payload must be a 2-tuple")
    try:
        created_at = datetime.fromisoformat(str(payload[0]))
        row_id = uuid.UUID(str(payload[1]))
    except ValueError as exc:
        raise InvalidCursorError(str(exc)) from exc
    return created_at, row_id


def encode_index_cursor(index: int) -> str:
    """Encode an integer position to an opaque token."""
    payload = [index]
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return _b64encode(raw)


def decode_index_cursor(cursor: str) -> int:
    """Decode a token from :func:`encode_index_cursor`."""
    try:
        raw = _b64decode(cursor)
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise InvalidCursorError(str(exc)) from exc
    if not isinstance(payload, list) or len(payload) != 1:
        raise InvalidCursorError("index cursor payload must be a 1-tuple")
    if not isinstance(payload[0], int) or payload[0] < 0:
        raise InvalidCursorError("index cursor must hold a non-negative int")
    return int(payload[0])


def invalid_cursor_error() -> HTTPException:
    """Return a 400 HTTPException with the canonical ``invalid_cursor`` body."""
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="invalid_cursor",
    )


async def paginate_keyset(
    session: AsyncSession,
    stmt: Select[tuple[RowT]],
    *,
    created_at_col: InstrumentedAttribute[datetime],
    id_col: InstrumentedAttribute[uuid.UUID],
    limit: int,
    cursor: str | None,
) -> tuple[Sequence[RowT], str | None]:
    """Apply a keyset cursor + ordering + limit to ``stmt`` and execute.

    Returns ``(items, next_cursor)``. ``next_cursor`` is ``None`` when the
    page is the last one. Decoded cursors that fail validation raise the
    canonical ``invalid_cursor`` HTTPException — callers can let that bubble.
    """
    if cursor is not None:
        try:
            cursor_created_at, cursor_id = decode_cursor(cursor)
        except InvalidCursorError as exc:
            raise invalid_cursor_error() from exc
        stmt = stmt.where(
            or_(
                created_at_col > cursor_created_at,
                and_(created_at_col == cursor_created_at, id_col > cursor_id),
            )
        )
    stmt = stmt.order_by(created_at_col, id_col).limit(limit + 1)
    rows = list((await session.execute(stmt)).scalars().all())
    next_cursor: str | None = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = encode_cursor(last.created_at, last.id)
        rows = rows[:limit]
    return rows, next_cursor


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "MIN_LIMIT",
    "CursorPage",
    "InvalidCursorError",
    "decode_cursor",
    "decode_index_cursor",
    "encode_cursor",
    "encode_index_cursor",
    "invalid_cursor_error",
    "paginate_keyset",
]
