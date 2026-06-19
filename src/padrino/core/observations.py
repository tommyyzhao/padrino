"""Observation builder — assembles the JSON observation given to a seat.

`build_observation(state, seat, event_log, ruleset)` returns a frozen Pydantic
:class:`Observation` matching ``prd.md`` §6.1, including all public events (up
to the recent-transcript cap), private events the seat is entitled to see, and
role-conditional fields (``mafia_teammates`` for mafia, ``previous_protected_target``
for the doctor, ``inspection_history`` for the detective).

Privacy rules enforced here:

* Town never sees mafia private chat or mafia kill submissions.
* Each seat sees only its own private submissions (PROTECT, INVESTIGATE,
  MAFIA_KILL, DetectiveResultDelivered, PrivateMessageSubmitted authored by
  self), plus — for mafia — any other mafia teammate's private events.
* SYSTEM events are not shown to players.

Pure function. Reads ``state``, ``seat``, ``event_log``, and the ruleset's
``Final[...]`` constants. No DB / LLM / clock / network access.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from padrino.core.engine.event_log import EventLog
from padrino.core.engine.legal_actions import LegalActions, legal_actions_for
from padrino.core.engine.state import GameState, Phase, Seat
from padrino.core.enums import Faction, PhaseKind, Role


class Ruleset(Protocol):
    """Structural ruleset Protocol exposing only the constants the builder needs."""

    RULESET_ID: str
    PUBLIC_MESSAGE_MAX_CHARS: int
    PRIVATE_MESSAGE_MAX_CHARS: int
    MEMORY_UPDATE_MAX_CHARS: int
    PUBLIC_TRANSCRIPT_RECENT_MESSAGE_LIMIT: int


class YouInfo(BaseModel):
    """The ``you`` block of the observation — the agent's own identity."""

    model_config = ConfigDict(frozen=True)

    player_id: str
    alive: bool
    role: Role
    faction: Faction


class DeadPlayerInfo(BaseModel):
    """One entry of ``dead_players`` — surfaces who died, when, and how."""

    model_config = ConfigDict(frozen=True)

    player_id: str
    day_or_night: str
    cause: str


class EventEntry(BaseModel):
    """A single observation-side event (public or private)."""

    model_config = ConfigDict(frozen=True)

    sequence: int
    phase: str
    event_type: str
    actor_player_id: str | None
    payload: dict[str, Any]


class MessageLimits(BaseModel):
    """Per-phase character limits surfaced to the agent."""

    model_config = ConfigDict(frozen=True)

    public_message_max_chars: int
    private_message_max_chars: int
    memory_update_max_chars: int


class InspectionResultEntry(BaseModel):
    """One row of the detective's ``inspection_history``."""

    model_config = ConfigDict(frozen=True)

    target: str
    finding: Literal["MAFIA", "TOWN"]
    phase: str


class SeatIdentity(BaseModel):
    """One per-seat identity disclosure entry (US-141, TRANSPARENT mode only).

    Carries ONLY the human-vs-AI / model-identity facts that a TRANSPARENT-mode
    game opts into disclosing. It deliberately carries NO ``role`` / ``faction``
    field: even in transparent mode another seat's hidden role/faction is never
    revealed mid-game (only its model / human identity is). In ANONYMOUS mode
    no :class:`SeatIdentity` ever reaches a seat — the whole
    ``identity_disclosure`` block is ``None``.
    """

    model_config = ConfigDict(frozen=True)

    public_player_id: str
    is_human: bool
    seat_kind: str | None = None
    model_id: str | None = None
    provider: str | None = None
    agent_build_id: str | None = None


class Observation(BaseModel):
    """Frozen JSON observation rendered into an LLM prompt for one seat."""

    model_config = ConfigDict(frozen=True)

    ruleset_id: str
    game_public_id: str
    phase: str
    day: int
    round: int
    you: YouInfo
    alive_players: tuple[str, ...]
    dead_players: tuple[DeadPlayerInfo, ...]
    public_events: tuple[EventEntry, ...]
    private_events: tuple[EventEntry, ...]
    legal_actions: LegalActions
    your_private_memory: str
    message_limits: MessageLimits
    mafia_teammates: tuple[str, ...] | None = None
    previous_protected_target: str | None = None
    inspection_history: tuple[InspectionResultEntry, ...] | None = None
    #: Per-seat human/model identity disclosure (US-141). ``None`` in ANONYMOUS
    #: mode (fail closed); a tuple of :class:`SeatIdentity` (possibly empty) in
    #: TRANSPARENT mode. Never carries another seat's role/faction.
    identity_disclosure: tuple[SeatIdentity, ...] | None = None


def format_phase_id(phase: Phase) -> str:
    """Render a :class:`Phase` as the canonical string used on event bodies."""
    kind = phase.kind
    if kind is PhaseKind.SETUP:
        return "SETUP"
    if kind is PhaseKind.TERMINAL:
        return "TERMINAL"
    if kind is PhaseKind.NIGHT_0_MAFIA_INTRO:
        return "NIGHT_0_MAFIA_INTRO"
    if kind is PhaseKind.DAY_DISCUSSION:
        return f"DAY_{phase.day}_DISCUSSION_ROUND_{phase.round}"
    if kind is PhaseKind.DAY_VOTE:
        return f"DAY_{phase.day}_VOTE"
    if kind is PhaseKind.NIGHT_MAFIA_DISCUSSION:
        return f"NIGHT_{phase.day}_MAFIA_DISCUSSION"
    if kind is PhaseKind.NIGHT_ACTIONS:
        return f"NIGHT_{phase.day}_ACTIONS"
    raise ValueError(f"Unknown PhaseKind: {kind!r}")  # pragma: no cover - defensive


def build_observation(
    state: GameState,
    seat: Seat,
    event_log: EventLog,
    ruleset: Ruleset,
    private_memory: str = "",
) -> Observation:
    """Build the per-seat observation for ``state.current_phase``."""
    alive_players = tuple(s.public_player_id for s in state.seats if s.alive)
    dead_players = _dead_players(event_log)
    public_events, private_events = _filter_events(state, seat, event_log, ruleset)

    you = YouInfo(
        player_id=seat.public_player_id,
        alive=seat.alive,
        role=seat.role,
        faction=seat.faction,
    )

    message_limits = MessageLimits(
        public_message_max_chars=ruleset.PUBLIC_MESSAGE_MAX_CHARS,
        private_message_max_chars=ruleset.PRIVATE_MESSAGE_MAX_CHARS,
        memory_update_max_chars=ruleset.MEMORY_UPDATE_MAX_CHARS,
    )

    mafia_teammates: tuple[str, ...] | None = None
    if seat.faction is Faction.MAFIA:
        mafia_teammates = tuple(
            s.public_player_id
            for s in state.seats
            if s.faction is Faction.MAFIA and s.public_player_id != seat.public_player_id
        )

    previous_protected_target: str | None = None
    if seat.role is Role.DOCTOR:
        previous_protected_target = seat.last_protected_target

    inspection_history: tuple[InspectionResultEntry, ...] | None = None
    if seat.role is Role.DETECTIVE:
        inspection_history = _inspection_history(seat, event_log)

    return Observation(
        ruleset_id=ruleset.RULESET_ID,
        game_public_id=state.game_id,
        phase=format_phase_id(state.current_phase),
        day=state.current_phase.day,
        round=state.current_phase.round,
        you=you,
        alive_players=alive_players,
        dead_players=dead_players,
        public_events=public_events,
        private_events=private_events,
        legal_actions=legal_actions_for(state, seat),
        your_private_memory=private_memory,
        message_limits=message_limits,
        mafia_teammates=mafia_teammates,
        previous_protected_target=previous_protected_target,
        inspection_history=inspection_history,
    )


def build_observation_for_mode(
    state: GameState,
    seat: Seat,
    event_log: EventLog,
    ruleset: Ruleset,
    *,
    identity_mode: Any,
    seat_identities: Sequence[SeatIdentity] | None = None,
    private_memory: str = "",
) -> Observation:
    """Build an identity-mode-aware per-seat observation (US-141).

    In :data:`~padrino.core.observation_privacy.ANONYMOUS` mode the result is
    byte-identical to :func:`build_observation` plus an explicit
    ``identity_disclosure=None``: a playing seat's view carries ZERO
    model/provider/agent-build identifiers AND zero human-vs-AI markers (the base
    observation already carries only the seat's OWN role/faction, hard rule 3).

    In :data:`~padrino.core.observation_privacy.TRANSPARENT` mode the result also
    surfaces ``identity_disclosure`` — a tuple of :class:`SeatIdentity` (the
    provided ``seat_identities`` or an empty tuple) carrying ONLY model / human
    identity, never another seat's hidden role/faction.

    A missing / ``None`` / unrecognised ``identity_mode`` FAILS CLOSED to
    anonymous (no disclosure). Pure function: no DB / LLM / clock / network.
    """
    # Local import: ``observation_privacy`` imports ``Observation`` from this
    # module, so a module-level import here would be circular.
    from padrino.core.observation_privacy import is_anonymous

    base = build_observation(state, seat, event_log, ruleset, private_memory)
    if is_anonymous(identity_mode):
        return base.model_copy(update={"identity_disclosure": None})
    disclosure = tuple(seat_identities) if seat_identities is not None else ()
    return base.model_copy(update={"identity_disclosure": disclosure})


def _entry_from_stored(stored_body: dict[str, Any], sequence: int) -> EventEntry:
    event_type = stored_body["event_type"]
    payload = stored_body.get("payload", {})
    # PRD §6.1 / line 476: role and faction are never revealed publicly
    # except via game outcomes. PlayerEliminated stores them on the chain for
    # transcript and replay, but the LLM-visible projection must strip them.
    if event_type == "PlayerEliminated":
        payload = {k: v for k, v in payload.items() if k not in {"role", "faction"}}
    return EventEntry(
        sequence=sequence,
        phase=stored_body["phase"],
        event_type=event_type,
        actor_player_id=stored_body.get("actor_player_id"),
        payload=payload,
    )


def _filter_events(
    state: GameState,
    seat: Seat,
    event_log: EventLog,
    ruleset: Ruleset,
) -> tuple[tuple[EventEntry, ...], tuple[EventEntry, ...]]:
    mafia_seat_ids = {s.public_player_id for s in state.seats if s.faction is Faction.MAFIA}
    seat_is_mafia = seat.faction is Faction.MAFIA
    seat_id = seat.public_player_id

    public: list[EventEntry] = []
    private: list[EventEntry] = []

    for stored in event_log.events:
        body = stored.body
        visibility = body.get("visibility")
        if visibility == "PUBLIC":
            public.append(_entry_from_stored(body, stored.sequence))
        elif visibility == "PRIVATE":
            actor = body.get("actor_player_id")
            if actor == seat_id or (seat_is_mafia and actor in mafia_seat_ids):
                private.append(_entry_from_stored(body, stored.sequence))

    limit = ruleset.PUBLIC_TRANSCRIPT_RECENT_MESSAGE_LIMIT
    if len(public) > limit:
        public = public[-limit:]

    return tuple(public), tuple(private)


def _dead_players(event_log: EventLog) -> tuple[DeadPlayerInfo, ...]:
    out: list[DeadPlayerInfo] = []
    for stored in event_log.events:
        body = stored.body
        if body.get("event_type") != "PlayerEliminated":
            continue
        payload = body["payload"]
        out.append(
            DeadPlayerInfo(
                player_id=payload["public_player_id"],
                day_or_night=body["phase"],
                cause=payload["cause"],
            )
        )
    return tuple(out)


def _inspection_history(seat: Seat, event_log: EventLog) -> tuple[InspectionResultEntry, ...]:
    out: list[InspectionResultEntry] = []
    for stored in event_log.events:
        body = stored.body
        if body.get("event_type") != "DetectiveResultDelivered":
            continue
        if body.get("actor_player_id") != seat.public_player_id:
            continue
        payload = body["payload"]
        out.append(
            InspectionResultEntry(
                target=payload["target"],
                finding=payload["finding"],
                phase=body["phase"],
            )
        )
    return tuple(out)


__all__ = [
    "DeadPlayerInfo",
    "EventEntry",
    "InspectionResultEntry",
    "MessageLimits",
    "Observation",
    "Ruleset",
    "SeatIdentity",
    "YouInfo",
    "build_observation",
    "build_observation_for_mode",
    "format_phase_id",
]
