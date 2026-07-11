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
* :func:`project_member` / :func:`roster_view` — the caller-safe roster shape.
  The roster carries NO principal id, NO seat_kind, NO per-seat human/AI map —
  only ``member_id``, ``display_label``, ``is_self``, ``host``, ``ready`` and
  ``present``.

All functions are data-in / data-out with an injected ``now``: no clock reads, no
DB, no IO. The impure api shell (``routes/lobbies.py``) loads the rows, calls
these with ``datetime.now(UTC)``, and applies the resulting eviction / cancel.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
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


class _PresenceMemberRow(Protocol):
    """The subset of a lobby member row needed for presence decisions."""

    id: uuid.UUID
    is_host: bool
    ready: bool
    joined_at: datetime
    last_seen_at: datetime | None


class _RosterMemberRow(_PresenceMemberRow, Protocol):
    """The subset of a lobby member row needed for roster projection."""

    principal_id: uuid.UUID


@dataclass(frozen=True)
class MemberView:
    """The safe player-facing view of one lobby member (US-330)."""

    member_id: uuid.UUID
    display_label: str
    is_self: bool
    host: bool
    ready: bool
    present: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "member_id": str(self.member_id),
            "display_label": self.display_label,
            "is_self": self.is_self,
            "host": self.host,
            "ready": self.ready,
            "present": self.present,
        }


def _last_active(member: _PresenceMemberRow) -> datetime:
    """The member's most recent liveness signal (heartbeat, else join time)."""
    raw = member.last_seen_at if member.last_seen_at is not None else member.joined_at
    return _as_aware(raw)


def is_present(member: _PresenceMemberRow, *, now: datetime, stale_seconds: float) -> bool:
    """True iff the member's last liveness signal is within the stale window."""
    cutoff = now - timedelta(seconds=stale_seconds)
    return _last_active(member) >= cutoff


def stale_member_ids(
    members: Iterable[_PresenceMemberRow], *, now: datetime, stale_seconds: float
) -> list[uuid.UUID]:
    """Members to evict: any non-present member is stale and dropped on read."""
    return [m.id for m in members if not is_present(m, now=now, stale_seconds=stale_seconds)]


def should_auto_cancel(
    *,
    lobby_updated_at: datetime,
    members: Iterable[_PresenceMemberRow],
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


def project_member(
    member: _RosterMemberRow,
    *,
    display_label: str,
    caller_principal_id: uuid.UUID,
    now: datetime,
    stale_seconds: float,
) -> MemberView:
    return MemberView(
        member_id=member.id,
        display_label=display_label,
        is_self=member.principal_id == caller_principal_id,
        host=member.is_host,
        ready=member.ready,
        present=is_present(member, now=now, stale_seconds=stale_seconds),
    )


def roster_view(
    members: Iterable[_RosterMemberRow],
    *,
    display_names_by_member_id: Mapping[uuid.UUID, str | None],
    caller_principal_id: uuid.UUID,
    now: datetime,
    stale_seconds: float,
) -> list[MemberView]:
    """Return the caller-safe roster: one :class:`MemberView` per member."""
    member_list = list(members)
    labels = _display_labels(member_list, display_names_by_member_id)
    return [
        project_member(
            m,
            display_label=labels[m.id],
            caller_principal_id=caller_principal_id,
            now=now,
            stale_seconds=stale_seconds,
        )
        for m in member_list
    ]


def _display_labels(
    members: list[_RosterMemberRow],
    display_names_by_member_id: Mapping[uuid.UUID, str | None],
) -> dict[uuid.UUID, str]:
    raw_labels: list[tuple[uuid.UUID, str, str | None, int]] = []
    duplicate_counts: dict[str, int] = {}
    for position, member in enumerate(members, start=1):
        raw = display_names_by_member_id.get(member.id)
        stripped = raw.strip() if raw is not None else ""
        normalized = stripped.casefold() if stripped else ""
        if normalized:
            duplicate_counts[normalized] = duplicate_counts.get(normalized, 0) + 1
        raw_labels.append((member.id, stripped, normalized or None, position))

    seen: dict[str, int] = {}
    labels: dict[uuid.UUID, str] = {}
    for member_id, stripped, label_key, position in raw_labels:
        if label_key is None:
            labels[member_id] = f"Player {position}"
            continue
        if duplicate_counts[label_key] == 1:
            labels[member_id] = stripped
            continue
        ordinal = seen.get(label_key, 0) + 1
        seen[label_key] = ordinal
        labels[member_id] = f"{stripped} #{ordinal}"
    return labels


__all__ = [
    "MemberView",
    "is_present",
    "project_member",
    "roster_view",
    "should_auto_cancel",
    "stale_member_ids",
]
