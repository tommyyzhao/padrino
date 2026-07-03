"""Pure ranked-handle name policy (US-297).

The impure shell owns storage lookups, uniqueness claims, and mutable moderation
lists. This module only validates one candidate handle against caller-injected
blocklist/reserved data and returns stable machine-readable reason tokens.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

MIN_RANKED_HANDLE_LENGTH = 3
MAX_RANKED_HANDLE_LENGTH = 24

_ALLOWED_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
_COMPACT_SEPARATORS = frozenset({"_", "-"})


class RankedHandleReason(StrEnum):
    """Machine-readable ranked-handle validation tokens."""

    OK = "ok"
    TOO_SHORT = "too_short"
    TOO_LONG = "too_long"
    BAD_CHARSET = "bad_charset"
    PROFANE = "profane"
    RESERVED = "reserved"


@dataclass(frozen=True, slots=True)
class RankedHandleDecision:
    """Result of validating one ranked-handle candidate."""

    ok: bool
    reason: RankedHandleReason
    handle: str | None = None
    normalized: str | None = None


def validate_ranked_handle(
    candidate: str | None,
    *,
    blocklist: Iterable[str],
    reserved: Iterable[str],
) -> RankedHandleDecision:
    """Validate one ranked handle using injected moderation lists.

    The normalized value is the storage-side uniqueness key. Callers should use
    it for case-insensitive atomic claims, not the display-preserving handle.
    """

    handle = "" if candidate is None else candidate.strip()
    if len(handle) < MIN_RANKED_HANDLE_LENGTH:
        return _refused(RankedHandleReason.TOO_SHORT)
    if len(handle) > MAX_RANKED_HANDLE_LENGTH:
        return _refused(RankedHandleReason.TOO_LONG)
    if any(ch not in _ALLOWED_CHARS for ch in handle):
        return _refused(RankedHandleReason.BAD_CHARSET)

    normalized = normalize_ranked_handle(handle)
    compact = _compact(normalized)
    if any(token in compact for token in _normalized_tokens(blocklist)):
        return _refused(RankedHandleReason.PROFANE)
    if any(_reserved_match(compact, token) for token in _normalized_tokens(reserved)):
        return _refused(RankedHandleReason.RESERVED)
    return RankedHandleDecision(
        ok=True,
        reason=RankedHandleReason.OK,
        handle=handle,
        normalized=normalized,
    )


def normalize_ranked_handle(handle: str) -> str:
    """Return the case-insensitive uniqueness key for ``handle``."""

    return handle.strip().casefold()


def _refused(reason: RankedHandleReason) -> RankedHandleDecision:
    return RankedHandleDecision(ok=False, reason=reason)


def _compact(value: str) -> str:
    return "".join(ch for ch in value if ch not in _COMPACT_SEPARATORS)


def _normalized_tokens(values: Iterable[str]) -> tuple[str, ...]:
    tokens: list[str] = []
    for value in values:
        token = _compact(normalize_ranked_handle(value))
        if token:
            tokens.append(token)
    return tuple(tokens)


def _reserved_match(compact_handle: str, reserved_token: str) -> bool:
    return (
        compact_handle == reserved_token
        or compact_handle.startswith(reserved_token)
        or compact_handle.endswith(reserved_token)
    )


__all__ = [
    "MAX_RANKED_HANDLE_LENGTH",
    "MIN_RANKED_HANDLE_LENGTH",
    "RankedHandleDecision",
    "RankedHandleReason",
    "normalize_ranked_handle",
    "validate_ranked_handle",
]
