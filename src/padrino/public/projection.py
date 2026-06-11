"""Versioned public_event_v1 projection contract (US-086).

Stable, versioned public projection of game events distinct from internal
core events. Internal schema changes never become public-API migrations because
consumers bind to this module's contract, not to raw DB columns.

This is an impure-adjacent, pure-ish module: it delegates all identity-blind
filtering to :func:`padrino.core.spectator_projection.project_event_for_spectator`
(the pure core) and then wraps the result in the stable versioned envelope.

Forbidden keys are surfaced as :data:`PUBLIC_EVENT_FORBIDDEN_KEYS` so callers
can assert against them — the set is identical to the ranked-observation guard's
deny list (:data:`padrino.core.observation_privacy.FORBIDDEN_PAYLOAD_KEYS`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

from padrino.core.observation_privacy import FORBIDDEN_PAYLOAD_KEYS
from padrino.core.spectator_projection import project_event_for_spectator

#: Keys that must never appear anywhere in a public_event_v1 payload.
#: Identical to the ranked-observation guard's deny list so a field that is
#: unsafe for a competing agent is also unsafe for a public spectator.
PUBLIC_EVENT_FORBIDDEN_KEYS: Final[frozenset[str]] = FORBIDDEN_PAYLOAD_KEYS

#: Exact set of top-level fields in every public_event_v1 dict.
PUBLIC_EVENT_V1_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "sequence",
        "phase",
        "event_type",
        "visibility",
        "actor_player_id",
        "payload",
        "prev_event_hash",
        "event_hash",
    }
)

_SCHEMA_VERSION: Final[str] = "public_event_v1"


def to_public_event_v1(event: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project one stored internal event to the stable ``public_event_v1`` shape.

    Returns ``None`` for events that must not be shown to public viewers (PRIVATE
    / SYSTEM / unknown visibility). For PUBLIC events, strips every
    :data:`PUBLIC_EVENT_FORBIDDEN_KEYS` entry from the payload (walking nested
    dicts and lists), then wraps the result in the versioned envelope.

    The returned dict has exactly the fields in :data:`PUBLIC_EVENT_V1_FIELDS` —
    no internal bookkeeping columns, no implementation details.

    Built on :func:`padrino.core.spectator_projection.project_event_for_spectator`
    which is the identity-blind pure-core guard; this function only adds the
    schema_version envelope and normalises field types.
    """
    projected = project_event_for_spectator(event)
    if projected is None:
        return None

    return {
        "schema_version": _SCHEMA_VERSION,
        "sequence": int(projected.get("sequence", 0)),
        "phase": str(projected.get("phase", "")),
        "event_type": str(projected.get("event_type", "")),
        "visibility": str(projected.get("visibility", "")),
        "actor_player_id": projected.get("actor_player_id"),
        "payload": dict(projected.get("payload", {})),
        "prev_event_hash": str(projected.get("prev_event_hash", "")),
        "event_hash": str(projected.get("event_hash", "")),
    }


def to_public_events_v1(events: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Project a sequence of stored events to the stable ``public_event_v1`` shape.

    Drops PRIVATE / SYSTEM events, strips forbidden payload keys from surviving
    PUBLIC events, and wraps each in the versioned envelope. Preserves order.
    """
    result: list[dict[str, Any]] = []
    for event in events:
        projected = to_public_event_v1(event)
        if projected is not None:
            result.append(projected)
    return result


__all__ = [
    "PUBLIC_EVENT_FORBIDDEN_KEYS",
    "PUBLIC_EVENT_V1_FIELDS",
    "to_public_event_v1",
    "to_public_events_v1",
]
