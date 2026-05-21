"""Offline privacy audit for a completed game's event log (US-078).

For each seat in the game, reconstructs the per-seat event projection (the
same visibility-filtered view :func:`padrino.core.observations.build_observation`
would produce) and runs it through
:func:`padrino.core.observation_privacy.audit_observation_log_for_seat`. The
collected :class:`LeakFinding` records describe every cross-seat leak that
would have surfaced to an agent during play.

This module is the impure wrapper: it walks the raw event-log bodies and
applies the visibility filter directly instead of rebuilding full
:class:`Observation` objects via the typed-event reducer. Pure-core then
performs the actual privacy check. The two layers share
:data:`padrino.core.observation_privacy.FORBIDDEN_PAYLOAD_KEYS` so a runtime
guard violation and an offline finding always reference the same forbidden
set.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from padrino.core.engine.event_log import EventLog, StoredEvent
from padrino.core.engine.legal_actions import LegalActions
from padrino.core.engine.state import Seat
from padrino.core.enums import Faction
from padrino.core.observation_privacy import (
    LeakFinding,
    audit_observation_log_for_seat,
)
from padrino.core.observations import (
    EventEntry,
    MessageLimits,
    Observation,
    YouInfo,
)
from padrino.core.rulesets import mini7_v1

# Same redaction the observation builder applies before handing payloads to
# the LLM: ``PlayerEliminated`` legitimately carries ``role`` and ``faction``
# on the chain for replay / transcript but the LLM-visible projection strips
# them. The audit must mirror this stripping so it doesn't flag the engine's
# own properly-scrubbed projection as a leak.
_PLAYER_ELIMINATED_STRIPPED_KEYS = frozenset({"role", "faction"})

_AUDIT_LEGAL_ACTIONS = LegalActions(allowed_action_types=[], legal_targets=[])
_AUDIT_MESSAGE_LIMITS = MessageLimits(
    public_message_max_chars=mini7_v1.PUBLIC_MESSAGE_MAX_CHARS,
    private_message_max_chars=mini7_v1.PRIVATE_MESSAGE_MAX_CHARS,
    memory_update_max_chars=mini7_v1.MEMORY_UPDATE_MAX_CHARS,
)


class AuditReport(BaseModel):
    """Aggregated audit findings across every seat in one game."""

    model_config = ConfigDict(frozen=True)

    findings: tuple[LeakFinding, ...]

    @property
    def finding_count(self) -> int:
        return len(self.findings)


def audit_ranked_observations(
    event_log: EventLog,
    seat_assignments: Sequence[Seat],
) -> AuditReport:
    """Audit the per-seat event projection for every seat in the game.

    For each seat, walks ``event_log.events`` once and applies the same
    visibility filter as :func:`padrino.core.observations._filter_events`
    (PUBLIC events are universally visible; PRIVATE events flow to their
    actor and to every mafia seat when the observing seat is also mafia;
    SYSTEM events stay hidden). The resulting tuple of :class:`EventEntry`
    objects is wrapped in a stub :class:`Observation` whose only meaningful
    fields are the event lists + ``game_public_id`` + ``you.player_id`` (the
    other slots are placeholders) and then handed to
    :func:`audit_observation_log_for_seat`.

    Returns an :class:`AuditReport` whose ``findings`` tuple is empty when
    the chain is clean. The audit never raises, never logs the raw leaked
    value, and never embeds payload contents in the findings beyond the
    type+length redaction.
    """
    if not seat_assignments:
        return AuditReport(findings=())

    game_id = _resolve_game_id(event_log.events)
    mafia_seat_ids = frozenset(
        s.public_player_id for s in seat_assignments if s.faction is Faction.MAFIA
    )

    findings: list[LeakFinding] = []
    for seat in seat_assignments:
        observation = _project_observation(event_log.events, seat, mafia_seat_ids, game_id)
        findings.extend(audit_observation_log_for_seat([observation], seat.public_player_id))
    return AuditReport(findings=tuple(findings))


def _resolve_game_id(events: Sequence[StoredEvent]) -> str:
    """Find the game's public id from the ``GameCreated`` event, if any."""
    for stored in events:
        body = stored.body
        if body.get("event_type") == "GameCreated":
            game_id = body.get("payload", {}).get("game_id")
            if isinstance(game_id, str):
                return game_id
    return ""


def _project_observation(
    events: Sequence[StoredEvent],
    seat: Seat,
    mafia_seat_ids: frozenset[str],
    game_id: str,
) -> Observation:
    seat_is_mafia = seat.faction is Faction.MAFIA
    seat_id = seat.public_player_id
    public: list[EventEntry] = []
    private: list[EventEntry] = []
    for stored in events:
        body = stored.body
        visibility = body.get("visibility")
        if visibility == "PUBLIC":
            public.append(_entry_for_audit(body, stored.sequence))
        elif visibility == "PRIVATE":
            actor = body.get("actor_player_id")
            if actor == seat_id or (seat_is_mafia and actor in mafia_seat_ids):
                private.append(_entry_for_audit(body, stored.sequence))

    return Observation(
        ruleset_id=mini7_v1.RULESET_ID,
        game_public_id=game_id,
        phase="AUDIT",
        day=0,
        round=0,
        you=YouInfo(
            player_id=seat_id,
            alive=seat.alive,
            role=seat.role,
            faction=seat.faction,
        ),
        alive_players=(),
        dead_players=(),
        public_events=tuple(public),
        private_events=tuple(private),
        legal_actions=_AUDIT_LEGAL_ACTIONS,
        your_private_memory="",
        message_limits=_AUDIT_MESSAGE_LIMITS,
    )


def _entry_for_audit(body: dict[str, object], sequence: int) -> EventEntry:
    event_type = body["event_type"]
    payload = body.get("payload", {})
    if not isinstance(payload, dict):
        payload = {}
    if event_type == "PlayerEliminated":
        payload = {k: v for k, v in payload.items() if k not in _PLAYER_ELIMINATED_STRIPPED_KEYS}
    return EventEntry(
        sequence=sequence,
        phase=str(body.get("phase", "")),
        event_type=str(event_type),
        actor_player_id=body.get("actor_player_id"),  # type: ignore[arg-type]
        payload=dict(payload),
    )


__all__ = ["AuditReport", "audit_ranked_observations"]
