"""Spectator projection — the single safe render path for a non-terminal game.

A spectator watching a *live* (in-progress) game must never learn hidden
information ahead of the players: roles, factions, who the mafia targeted, who
the doctor protected, detective findings, or private mafia chat. The
deterministic engine encodes that hidden information in three places
(see :mod:`padrino.core.engine.events`):

* **PRIVATE** events — private mafia chat, the mafia kill vote, the doctor
  protect, the detective investigation, and the detective result.
* **SYSTEM** events — ``RolesAssigned`` carries every seat's ``role`` +
  ``faction``; ``NightResolved`` carries the mafia kill target + doctor
  protection *before* the public elimination is announced.
* the ``role`` / ``faction`` keys baked into the otherwise-**PUBLIC**
  ``PlayerEliminated`` payload.

This module is the **only** code that should render a non-terminal game to a
non-player (anonymous spectators *and* authenticated readers without the admin
token). It keeps PUBLIC events, drops PRIVATE and SYSTEM events whole, and
strips every :data:`SPECTATOR_FORBIDDEN_PAYLOAD_KEYS` entry — which already
includes ``role`` and ``faction`` — from the surviving PUBLIC payloads. The
forbidden-key set is shared with the ranked-observation guard
(:data:`padrino.core.observation_privacy.FORBIDDEN_PAYLOAD_KEYS`) so a field
that is unsafe for a competing agent is also unsafe for a spectator.

Pure function. No DB / LLM / clock / network access.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

from padrino.core.observation_privacy import FORBIDDEN_PAYLOAD_KEYS

#: The only event visibility a non-terminal game exposes to a spectator.
SPECTATOR_VISIBLE_VISIBILITY: Final[str] = "PUBLIC"

#: Visibilities dropped wholesale from a live spectator view (PRIVATE chat /
#: night submissions / detective findings; SYSTEM role assignments + night
#: resolution). Anything not equal to :data:`SPECTATOR_VISIBLE_VISIBILITY` is
#: dropped, so an unknown / malformed visibility fails closed.
SPECTATOR_DROP_VISIBILITIES: Final[frozenset[str]] = frozenset({"PRIVATE", "SYSTEM"})

#: Payload keys stripped from surviving PUBLIC events. Reuses the ranked guard's
#: set, which already contains ``role`` + ``faction`` (the leak inside a PUBLIC
#: ``PlayerEliminated`` payload) plus model-identity / rating carriers.
SPECTATOR_FORBIDDEN_PAYLOAD_KEYS: Final[frozenset[str]] = FORBIDDEN_PAYLOAD_KEYS


def strip_forbidden(value: Any) -> Any:
    """Return ``value`` with every :data:`SPECTATOR_FORBIDDEN_PAYLOAD_KEYS` entry removed.

    Walks nested dicts, lists, and tuples; scalars are returned unchanged.
    Forbidden keys are dropped (not replaced with a sentinel) so the spectator
    surface looks like the hidden field never existed.
    """
    if isinstance(value, Mapping):
        return {
            k: strip_forbidden(v)
            for k, v in value.items()
            if k not in SPECTATOR_FORBIDDEN_PAYLOAD_KEYS
        }
    if isinstance(value, list):
        return [strip_forbidden(item) for item in value]
    if isinstance(value, tuple):
        return tuple(strip_forbidden(item) for item in value)
    return value


def project_event_for_spectator(event: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project one stored event for a live (non-terminal) spectator view.

    Returns ``None`` to drop the event entirely (any non-PUBLIC visibility);
    otherwise returns a shallow copy of the event with its ``payload``
    forbidden-key stripped. All other top-level fields (sequence, event_type,
    phase, hashes, …) are preserved verbatim.
    """
    visibility = str(event.get("visibility", "")).upper()
    if visibility != SPECTATOR_VISIBLE_VISIBILITY:
        return None
    stripped = strip_forbidden(event.get("payload", {}))
    if not isinstance(stripped, dict):
        stripped = {}
    return {**event, "payload": stripped}


def project_events_for_spectator(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Project a sequence of stored events for a live spectator view.

    Drops PRIVATE/SYSTEM events and strips forbidden keys from the surviving
    PUBLIC payloads, preserving order.
    """
    projected: list[dict[str, Any]] = []
    for event in events:
        kept = project_event_for_spectator(event)
        if kept is not None:
            projected.append(kept)
    return projected


__all__ = [
    "SPECTATOR_DROP_VISIBILITIES",
    "SPECTATOR_FORBIDDEN_PAYLOAD_KEYS",
    "SPECTATOR_VISIBLE_VISIBILITY",
    "project_event_for_spectator",
    "project_events_for_spectator",
    "strip_forbidden",
]
