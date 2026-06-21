"""Lobby presence + idle-lifecycle decisions and identity-blind roster (US-148).

This module holds the PURE decision logic for the private-lobby presence and
auto-cancel lifecycle, plus the identity-blind roster projection that the join /
roster / ready / state-channel surfaces all share:

* :func:`is_present` — a member is present iff it heartbeated within the stale
  window (a member with no recorded heartbeat is treated by its ``joined_at``).
* :func:`stale_member_ids` — which members should be evicted on the next read.
* :func:`should_auto_cancel` — whether an idle / host-abandoned lobby must
  transition to ``CLOSED`` (idle = no activity within the idle window; abandoned
  = the host is no longer present and nobody is).
* :func:`project_member` / :func:`roster_view` — the counts-only, identity-blind
  roster shape. The roster carries NO principal id, NO seat_kind, NO per-seat
  human/AI map — only ``member_id``, ``is_host``, ``ready`` and ``present``.

All functions are data-in / data-out with an injected ``now``: no clock reads, no
DB, no IO. The impure api shell (``routes/lobbies.py``) loads the rows, calls
these with ``datetime.now(UTC)``, and applies the resulting eviction / cancel.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol


def _as_aware(value: datetime) -> datetime:
    """Coerce a SQLite-naive ``DateTime(timezone=True)`` value to UTC-aware.

    SQLite drops tzinfo on a stored timezone-aware column, so a member's
    ``joined_at`` / ``last_seen_at`` loads back naive; comparing it against an
    aware ``now`` would raise. Pure data coercion, no clock read.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class _MemberRow(Protocol):
    """The subset of :class:`padrino.db.models.LobbyMember` these helpers read."""

    id: uuid.UUID
    is_host: bool
    ready: bool
    joined_at: datetime
    last_seen_at: datetime | None


@dataclass(frozen=True)
class MemberView:
    """The identity-blind, counts-only view of one lobby member (US-148)."""

    member_id: uuid.UUID
    is_host: bool
    ready: bool
    present: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "member_id": str(self.member_id),
            "is_host": self.is_host,
            "ready": self.ready,
            "present": self.present,
        }


def _last_active(member: _MemberRow) -> datetime:
    """The member's most recent liveness signal (heartbeat, else join time)."""
    raw = member.last_seen_at if member.last_seen_at is not None else member.joined_at
    return _as_aware(raw)


def is_present(member: _MemberRow, *, now: datetime, stale_seconds: float) -> bool:
    """True iff the member's last liveness signal is within the stale window."""
    cutoff = now - timedelta(seconds=stale_seconds)
    return _last_active(member) >= cutoff


def stale_member_ids(
    members: Iterable[_MemberRow], *, now: datetime, stale_seconds: float
) -> list[uuid.UUID]:
    """Members to evict: any non-present member is stale and dropped on read."""
    return [m.id for m in members if not is_present(m, now=now, stale_seconds=stale_seconds)]


def should_auto_cancel(
    *,
    lobby_updated_at: datetime,
    members: Iterable[_MemberRow],
    now: datetime,
    idle_seconds: float,
    stale_seconds: float,
) -> bool:
    """Whether an OPEN/LOCKED lobby must auto-cancel to ``CLOSED``.

    A lobby auto-cancels when EITHER it has had no activity within the idle
    window (``lobby_updated_at`` is older than ``idle_seconds``) OR the host has
    abandoned it — there is no present member at all (every member, including the
    host, is stale). The caller restricts this to non-terminal lobbies.
    """
    member_list = list(members)
    idle_cutoff = now - timedelta(seconds=idle_seconds)
    if lobby_updated_at < idle_cutoff:
        return True
    any_present = any(is_present(m, now=now, stale_seconds=stale_seconds) for m in member_list)
    return not any_present


def project_member(member: _MemberRow, *, now: datetime, stale_seconds: float) -> MemberView:
    return MemberView(
        member_id=member.id,
        is_host=member.is_host,
        ready=member.ready,
        present=is_present(member, now=now, stale_seconds=stale_seconds),
    )


def roster_view(
    members: Iterable[_MemberRow], *, now: datetime, stale_seconds: float
) -> list[MemberView]:
    """The identity-blind roster: one :class:`MemberView` per member."""
    return [project_member(m, now=now, stale_seconds=stale_seconds) for m in members]


__all__ = [
    "MemberView",
    "is_present",
    "project_member",
    "roster_view",
    "should_auto_cancel",
    "stale_member_ids",
]
