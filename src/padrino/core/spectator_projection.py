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

from padrino.core.observation_privacy import (
    FORBIDDEN_PAYLOAD_KEYS,
    IDENTITY_MARKER_KEYS,
    is_anonymous,
)

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

#: Payload keys stripped from a PUBLIC event in TRANSPARENT mode (US-141). A
#: transparent game opts into disclosing human-vs-AI / model identity, so those
#: markers (:data:`IDENTITY_MARKER_KEYS`) are allowed through — but every OTHER
#: forbidden key (notably the pre-reveal ``role`` / ``faction`` baked into a
#: ``PlayerEliminated`` payload, and rating carriers) is still stripped, because
#: a hidden role/faction is never revealed mid-game even in transparent mode.
SPECTATOR_TRANSPARENT_FORBIDDEN_PAYLOAD_KEYS: Final[frozenset[str]] = (
    FORBIDDEN_PAYLOAD_KEYS - IDENTITY_MARKER_KEYS
)


def strip_forbidden(value: Any, forbidden: frozenset[str] | None = None) -> Any:
    """Return ``value`` with every ``forbidden`` key removed (recursively).

    ``forbidden`` defaults to :data:`SPECTATOR_FORBIDDEN_PAYLOAD_KEYS` (the
    anonymous, fail-closed set). Walks nested dicts, lists, and tuples; scalars
    are returned unchanged. Forbidden keys are dropped (not replaced with a
    sentinel) so the spectator surface looks like the hidden field never existed.
    """
    keys = SPECTATOR_FORBIDDEN_PAYLOAD_KEYS if forbidden is None else forbidden
    if isinstance(value, Mapping):
        return {k: strip_forbidden(v, keys) for k, v in value.items() if k not in keys}
    if isinstance(value, list):
        return [strip_forbidden(item, keys) for item in value]
    if isinstance(value, tuple):
        return tuple(strip_forbidden(item, keys) for item in value)
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


def project_event_for_spectator_mode(
    event: Mapping[str, Any],
    *,
    identity_mode: Any,
) -> dict[str, Any] | None:
    """Project one stored event for a spectator, identity-mode aware (US-141).

    In ANONYMOUS mode (the fail-closed default for a missing / ``None`` /
    unrecognised ``identity_mode``) this is identical to
    :func:`project_event_for_spectator`: every
    :data:`SPECTATOR_FORBIDDEN_PAYLOAD_KEYS` entry is stripped, so no
    model/provider identity and no human-vs-AI marker survives.

    In TRANSPARENT mode the human-vs-AI / model-identity markers
    (:data:`~padrino.core.observation_privacy.IDENTITY_MARKER_KEYS`) are allowed
    through, but every other forbidden key — notably the pre-reveal
    ``role`` / ``faction`` baked into a ``PlayerEliminated`` payload — is still
    stripped (a hidden role is never revealed mid-game even when transparent).
    """
    visibility = str(event.get("visibility", "")).upper()
    if visibility != SPECTATOR_VISIBLE_VISIBILITY:
        return None
    forbidden = (
        SPECTATOR_FORBIDDEN_PAYLOAD_KEYS
        if is_anonymous(identity_mode)
        else SPECTATOR_TRANSPARENT_FORBIDDEN_PAYLOAD_KEYS
    )
    stripped = strip_forbidden(event.get("payload", {}), forbidden)
    if not isinstance(stripped, dict):
        stripped = {}
    return {**event, "payload": stripped}


def project_events_for_spectator_mode(
    events: Iterable[Mapping[str, Any]],
    *,
    identity_mode: Any,
) -> list[dict[str, Any]]:
    """Mode-aware sequence projection (see :func:`project_event_for_spectator_mode`)."""
    projected: list[dict[str, Any]] = []
    for event in events:
        kept = project_event_for_spectator_mode(event, identity_mode=identity_mode)
        if kept is not None:
            projected.append(kept)
    return projected


__all__ = [
    "SPECTATOR_DROP_VISIBILITIES",
    "SPECTATOR_FORBIDDEN_PAYLOAD_KEYS",
    "SPECTATOR_TRANSPARENT_FORBIDDEN_PAYLOAD_KEYS",
    "SPECTATOR_VISIBLE_VISIBILITY",
    "project_event_for_spectator",
    "project_event_for_spectator_mode",
    "project_events_for_spectator",
    "project_events_for_spectator_mode",
    "strip_forbidden",
]
