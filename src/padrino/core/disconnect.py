"""Disconnect-grace decisions + identity-blind presence projection (US-150).

When a human drops, they get a short reconnect **grace window**; if the window
expires a curated AI silently takes the seat so the game continues. This module
holds the PURE decision logic for that lifecycle plus the identity-blind
presence projection a viewer's client receives:

* :func:`is_within_grace` — a disconnected seat is still reconnectable iff it
  dropped within the grace window (measured against an injected ``now``).
* :func:`seats_past_grace` — which disconnected seats have exhausted their grace
  and must be taken over by an AI.
* :func:`project_presence_for_viewer` — the presence frame a viewer's client may
  see. In ANONYMOUS mode it carries ONLY the viewer's OWN presence: other seats'
  presence / reconnecting state is never exposed (AIs do not disconnect, so any
  per-seat presence signal would out a human seat). In TRANSPARENT mode it may
  carry every seat's presence.

Every function is data-in / data-out with an injected ``now``: no clock reads,
no random, no DB, no IO. The impure runner (:mod:`padrino.runner.disconnect_takeover`)
records heartbeats, calls these with a wall-clock ``now``, and performs the
swap + ``SeatTakenOver`` emission when :func:`seats_past_grace` returns a seat.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from padrino.core.observation_privacy import is_anonymous


@dataclass(frozen=True, slots=True)
class SeatPresence:
    """A seat's connection state for the disconnect-grace lifecycle.

    ``connected`` is the live transport's view of whether the human's client is
    currently attached. ``disconnected_at`` is the wall-clock instant the seat
    last dropped (``None`` while connected, or for an AI seat that never had a
    human client). Both are plain data supplied by the impure transport layer;
    this module never reads a clock itself.
    """

    public_player_id: str
    connected: bool
    disconnected_at: datetime | None = None


def _as_aware(value: datetime) -> datetime:
    """Coerce a possibly tz-naive stored timestamp to UTC-aware.

    A ``DateTime(timezone=True)`` column loads back tz-naive on SQLite, so a
    ``disconnected_at`` persisted as UTC-aware can arrive naive; comparing it
    against an aware ``now`` would raise. Pure data coercion, no clock read.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def is_within_grace(presence: SeatPresence, *, now: datetime, grace_seconds: float) -> bool:
    """True iff a disconnected seat may still reconnect (still in grace).

    A connected seat is trivially within grace (nothing to reclaim). A
    disconnected seat is within grace iff it dropped no longer ago than
    ``grace_seconds``; a disconnected seat with no recorded drop instant is
    treated as just-dropped (still reconnectable).
    """
    if presence.connected:
        return True
    if presence.disconnected_at is None:
        return True
    cutoff = now - timedelta(seconds=grace_seconds)
    return _as_aware(presence.disconnected_at) >= cutoff


def seats_past_grace(
    presences: Iterable[SeatPresence], *, now: datetime, grace_seconds: float
) -> list[str]:
    """Seat ids whose grace window expired and must be taken over by an AI.

    Returns the disconnected seats that are no longer within grace, in input
    order. A reconnect BEFORE this fires returns the seat to the human (the seat
    flips back to ``connected`` and is never listed); a takeover AFTER it fires
    is the silent AI swap the runner performs.
    """
    return [
        p.public_player_id
        for p in presences
        if not is_within_grace(p, now=now, grace_seconds=grace_seconds)
    ]


def project_presence_for_viewer(
    presences: Iterable[SeatPresence],
    *,
    viewer_seat_id: str,
    identity_mode: object,
) -> list[dict[str, object]]:
    """The presence frame a viewer's client may see, identity-blind by default.

    In ANONYMOUS mode (the default; a missing/None mode fails closed to
    anonymous) the frame contains ONLY the viewer's OWN seat presence — other
    seats' presence / reconnecting state is never exposed, because an AI never
    disconnects and so any per-seat presence signal would out a human seat. In
    TRANSPARENT mode the frame may carry every seat's presence.

    Each entry is ``{seat_id, connected}`` only — never a drop timestamp or any
    human/AI marker.
    """
    presence_list = list(presences)
    if is_anonymous(identity_mode):
        visible = [p for p in presence_list if p.public_player_id == viewer_seat_id]
    else:
        visible = presence_list
    return [{"seat_id": p.public_player_id, "connected": p.connected} for p in visible]


__all__ = [
    "SeatPresence",
    "is_within_grace",
    "project_presence_for_viewer",
    "seats_past_grace",
]
