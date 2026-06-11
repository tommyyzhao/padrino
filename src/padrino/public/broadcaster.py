"""Broadcaster core: deterministic paced stream generator (US-088).

Pure function that turns a game's committed events into a paced sequence of
public_event_v1 frames with inter-frame delays. Delays are data — no clock
reads, no sleeps, no DB access. The transport layer (US-089) applies them.

Cadence defaults are loaded from Settings (``padrino_broadcast_cadence_*``)
via :func:`default_cadence`; pure unit tests inject a ``CadenceConfig``
directly and never touch Settings.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from padrino.public.projection import to_public_event_v1

#: Event types that represent public chat turns — typically the longest delay
#: because each message should feel like a real-time utterance.
_CHAT_EVENT_TYPES: frozenset[str] = frozenset({"PublicMessageSubmitted"})

#: Phase-boundary events that mark the transition between game phases.
_PHASE_EVENT_TYPES: frozenset[str] = frozenset({"PhaseStarted", "PhaseResolved"})

#: Dramatic elimination reveals — warrant the longest pause.
_ELIMINATION_EVENT_TYPES: frozenset[str] = frozenset({"PlayerEliminated"})

#: Vote/night resolution announcements.
_RESOLUTION_EVENT_TYPES: frozenset[str] = frozenset({"DayVoteResolved", "NightResolved"})


@dataclass(frozen=True)
class CadenceConfig:
    """Per-event-type delay configuration (milliseconds).

    All fields have sane defaults; override via Settings for production tuning
    without a code change.
    """

    chat_ms: int = 2500
    phase_ms: int = 3000
    elimination_ms: int = 4000
    resolution_ms: int = 3500
    default_ms: int = 1500


@dataclass(frozen=True)
class BroadcastFrame:
    """One frame in the broadcast plan.

    ``event`` is a ``public_event_v1`` dict (output of :func:`to_public_event_v1`).
    ``delay_ms`` is the pause the transport layer MUST apply before emitting
    this frame; it is not pre-applied here.
    """

    event: dict[str, Any]
    delay_ms: int


def _delay_for_event_type(event_type: str, cadence: CadenceConfig) -> int:
    if event_type in _CHAT_EVENT_TYPES:
        return cadence.chat_ms
    if event_type in _PHASE_EVENT_TYPES:
        return cadence.phase_ms
    if event_type in _ELIMINATION_EVENT_TYPES:
        return cadence.elimination_ms
    if event_type in _RESOLUTION_EVENT_TYPES:
        return cadence.resolution_ms
    return cadence.default_ms


def plan_broadcast(
    events: Iterable[Mapping[str, Any]],
    cadence: CadenceConfig,
) -> list[BroadcastFrame]:
    """Project internal events into a deterministic paced broadcast plan.

    Pure: no I/O, no clock reads, no DB access. PRIVATE/SYSTEM events are
    silently dropped (via :func:`to_public_event_v1`). Each surviving PUBLIC
    event becomes one :class:`BroadcastFrame` whose ``delay_ms`` is resolved
    from the event type against ``cadence``. Input order is preserved.

    Args:
        events:  Sequence of raw stored event dicts (Mapping).
        cadence: Delay config; pass ``default_cadence()`` in production or an
                 explicit ``CadenceConfig`` in tests.

    Returns:
        A list of :class:`BroadcastFrame` objects, one per PUBLIC event.
    """
    frames: list[BroadcastFrame] = []
    for event in events:
        projected = to_public_event_v1(event)
        if projected is None:
            continue
        event_type = projected.get("event_type", "")
        delay = _delay_for_event_type(event_type, cadence)
        frames.append(BroadcastFrame(event=projected, delay_ms=delay))
    return frames


def default_cadence() -> CadenceConfig:
    """Return a :class:`CadenceConfig` populated from the application settings.

    Deferred import keeps ``settings`` out of the module-level import graph so
    tests that don't need Settings can stay lightweight.
    """
    from padrino.settings import get_settings

    s = get_settings()
    return CadenceConfig(
        chat_ms=s.padrino_broadcast_cadence_chat_ms,
        phase_ms=s.padrino_broadcast_cadence_phase_ms,
        elimination_ms=s.padrino_broadcast_cadence_elimination_ms,
        resolution_ms=s.padrino_broadcast_cadence_resolution_ms,
        default_ms=s.padrino_broadcast_cadence_default_ms,
    )


__all__ = [
    "_CHAT_EVENT_TYPES",
    "_ELIMINATION_EVENT_TYPES",
    "_PHASE_EVENT_TYPES",
    "_RESOLUTION_EVENT_TYPES",
    "BroadcastFrame",
    "CadenceConfig",
    "default_cadence",
    "plan_broadcast",
]
