"""Pure ranked-ban/suspension predicate (US-300)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class ComparableInstant(Protocol):
    """Minimal protocol for injected instant values that can be ordered."""

    def __gt__(self, other: Any, /) -> bool: ...


class RankedBanKind(StrEnum):
    """Ranked enforcement primitive kinds."""

    BAN = "ban"
    SUSPENSION = "suspension"


@dataclass(frozen=True, slots=True)
class RankedBanSnapshot:
    """Clock-free ranked-ban facts consumed by the pure predicate."""

    kind: RankedBanKind | str
    expires_at: ComparableInstant | None
    lifted_at: ComparableInstant | None


def is_ranked_banned(active_bans: Iterable[RankedBanSnapshot], now: ComparableInstant) -> bool:
    """Return whether any supplied ranked ban is active at ``now``.

    The clock is injected by the shell. Lifted rows are ignored, permanent bans
    have no expiry, and expiring rows are active strictly before ``expires_at``.
    """

    for ban in active_bans:
        if ban.lifted_at is not None:
            continue
        if ban.kind == RankedBanKind.BAN or ban.kind == RankedBanKind.BAN.value:
            if ban.expires_at is None or ban.expires_at > now:
                return True
            continue
        if (
            ban.kind == RankedBanKind.SUSPENSION or ban.kind == RankedBanKind.SUSPENSION.value
        ) and (ban.expires_at is not None and ban.expires_at > now):
            return True
    return False


__all__ = [
    "RankedBanKind",
    "RankedBanSnapshot",
    "is_ranked_banned",
]
