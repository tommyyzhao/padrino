"""Impure shared clock and JSON helpers for shell code (never import from core/)."""

from __future__ import annotations

import json
from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return the current wall-clock instant in UTC."""
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Normalise a datetime to UTC; naive values are assumed to already be UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def canonical_json(value: object) -> str:
    """Deterministic JSON used for hash/signature inputs. Do not change the bytes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
