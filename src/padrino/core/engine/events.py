"""Typed event catalogue for the deterministic engine.

Every state change the engine produces lands here as a frozen Pydantic model.
The top-level :data:`Event` discriminated union is the single contract that
storage (US-016/US-032) and replay (US-018) share, so adding a new event type
means updating exactly one file.

All payloads are strict typed Pydantic models — no ``Any``. Server timestamps
and the hash chain fields are intentionally *absent* from these models: they
live on the storage envelope built by :mod:`padrino.core.engine.event_log`.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from padrino.core.enums import Faction, Role, SeatKind

Visibility = Literal["PUBLIC", "PRIVATE", "SYSTEM"]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True)


# --------------------------------------------------------------------------- #
# Payloads
# --------------------------------------------------------------------------- #


class SeatAssignment(_FrozenModel):
    """One row of the RolesAssigned payload."""

    public_player_id: str
    seat_index: int
    role: Role
    faction: Faction
    # Wave 9 (US-122): optional occupancy kind as pure provenance data. Defaults
    # to None so an existing AI-only event log is byte-identical and replays to
    # the same state; seat_kind never influences mechanics.
    seat_kind: SeatKind | None = None


class GameCreatedPayload(_FrozenModel):
    ruleset_id: str
    game_id: str
    game_seed: str
    player_count: int


class RolesAssignedPayload(_FrozenModel):
    assignments: tuple[SeatAssignment, ...]


class PhaseStartedPayload(_FrozenModel):
    phase_kind: str
    day: int
    round: int


class PublicMessageSubmittedPayload(_FrozenModel):
    text: str
    round_index: int | None = None
    # US-123: a HUMAN seat's message carries ONLY this opaque content reference
    # (sha256 of the raw text); the raw/cleaned text lives in the
    # ``human_chat_messages`` sidecar so it can be GDPR-redacted without changing
    # any event_hash. None for AI seats (whose text lives inline as before), so
    # existing/AI events stay byte-identical.
    content_ref: str | None = None


class PrivateMessageSubmittedPayload(_FrozenModel):
    text: str
    channel_id: str
    # US-123: see PublicMessageSubmittedPayload.content_ref.
    content_ref: str | None = None


class VoteSubmittedPayload(_FrozenModel):
    target: str | None
    is_abstain: bool


class MafiaKillVoteSubmittedPayload(_FrozenModel):
    target: str | None


class ProtectSubmittedPayload(_FrozenModel):
    target: str | None


class InvestigateSubmittedPayload(_FrozenModel):
    target: str | None


class RoleblockSubmittedPayload(_FrozenModel):
    target: str | None


class FrameSubmittedPayload(_FrozenModel):
    target: str | None


class TrackSubmittedPayload(_FrozenModel):
    target: str | None


class WatchSubmittedPayload(_FrozenModel):
    target: str | None


class CleanSubmittedPayload(_FrozenModel):
    target: str | None


class NightFeedbackDeliveredPayload(_FrozenModel):
    code: str
    target: str | None = None
    finding: Literal["MAFIA", "TOWN"] | None = None
    visited_player_ids: tuple[str, ...] = ()
    visitor_player_ids: tuple[str, ...] = ()


class ActionTimedOutPayload(_FrozenModel):
    expected_action_type: str
    defaulted_to: str


class OutputTruncatedPayload(_FrozenModel):
    reason: str
    raw_byte_length: int


class OutputInvalidPayload(_FrozenModel):
    reason: str
    validation_errors: tuple[str, ...]


class DayVoteResolvedPayload(_FrozenModel):
    eliminated: str | None
    vote_tally: dict[str, int]
    reason: str


class NightResolvedPayload(_FrozenModel):
    eliminated: str | None
    protected: str | None
    mafia_kill_target: str | None
    cleaned_deaths: tuple[str, ...] = ()
    clean_spent_actor_ids: tuple[str, ...] = ()


class DetectiveResultDeliveredPayload(_FrozenModel):
    target: str
    finding: Literal["MAFIA", "TOWN"]


class PlayerEliminatedPayload(_FrozenModel):
    public_player_id: str
    cause: str
    role: Role | None = None
    faction: Faction | None = None


class PhaseResolvedPayload(_FrozenModel):
    resolved_phase: str


class GameTerminatedPayload(_FrozenModel):
    winner: str
    reason: str


class RoleClaimedPayload(_FrozenModel):
    claimed_role: str


class SeatTakenOverPayload(_FrozenModel):
    """A silent AI takeover of a human seat.

    Provenance-only: folding this event preserves all mechanical state. The
    payload carries no wall-clock or random value — ``day`` and ``phase`` come
    from the engine's logical phase, and ``replacement_agent_build_ref`` is a
    caller-supplied identifier for the AI that assumed the seat.
    """

    public_player_id: str
    day: int
    phase: str
    reason: str
    replacement_agent_build_ref: str


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #


class GameCreated(_FrozenModel):
    event_type: Literal["GameCreated"] = "GameCreated"
    sequence: int
    phase: str
    visibility: Literal["SYSTEM"] = "SYSTEM"
    actor_player_id: str | None = None
    payload: GameCreatedPayload


class RolesAssigned(_FrozenModel):
    event_type: Literal["RolesAssigned"] = "RolesAssigned"
    sequence: int
    phase: str
    visibility: Literal["SYSTEM"] = "SYSTEM"
    actor_player_id: str | None = None
    payload: RolesAssignedPayload


class PhaseStarted(_FrozenModel):
    event_type: Literal["PhaseStarted"] = "PhaseStarted"
    sequence: int
    phase: str
    visibility: Literal["SYSTEM"] = "SYSTEM"
    actor_player_id: str | None = None
    payload: PhaseStartedPayload


class PublicMessageSubmitted(_FrozenModel):
    event_type: Literal["PublicMessageSubmitted"] = "PublicMessageSubmitted"
    sequence: int
    phase: str
    visibility: Literal["PUBLIC"] = "PUBLIC"
    actor_player_id: str
    payload: PublicMessageSubmittedPayload


class PrivateMessageSubmitted(_FrozenModel):
    event_type: Literal["PrivateMessageSubmitted"] = "PrivateMessageSubmitted"
    sequence: int
    phase: str
    visibility: Literal["PRIVATE"] = "PRIVATE"
    actor_player_id: str
    payload: PrivateMessageSubmittedPayload


class VoteSubmitted(_FrozenModel):
    event_type: Literal["VoteSubmitted"] = "VoteSubmitted"
    sequence: int
    phase: str
    visibility: Literal["PUBLIC"] = "PUBLIC"
    actor_player_id: str
    payload: VoteSubmittedPayload


class MafiaKillVoteSubmitted(_FrozenModel):
    event_type: Literal["MafiaKillVoteSubmitted"] = "MafiaKillVoteSubmitted"
    sequence: int
    phase: str
    visibility: Literal["PRIVATE"] = "PRIVATE"
    actor_player_id: str
    payload: MafiaKillVoteSubmittedPayload


class ProtectSubmitted(_FrozenModel):
    event_type: Literal["ProtectSubmitted"] = "ProtectSubmitted"
    sequence: int
    phase: str
    visibility: Literal["PRIVATE"] = "PRIVATE"
    actor_player_id: str
    payload: ProtectSubmittedPayload


class InvestigateSubmitted(_FrozenModel):
    event_type: Literal["InvestigateSubmitted"] = "InvestigateSubmitted"
    sequence: int
    phase: str
    visibility: Literal["PRIVATE"] = "PRIVATE"
    actor_player_id: str
    payload: InvestigateSubmittedPayload


class RoleblockSubmitted(_FrozenModel):
    event_type: Literal["RoleblockSubmitted"] = "RoleblockSubmitted"
    sequence: int
    phase: str
    visibility: Literal["PRIVATE"] = "PRIVATE"
    actor_player_id: str
    payload: RoleblockSubmittedPayload


class FrameSubmitted(_FrozenModel):
    event_type: Literal["FrameSubmitted"] = "FrameSubmitted"
    sequence: int
    phase: str
    visibility: Literal["PRIVATE"] = "PRIVATE"
    actor_player_id: str
    payload: FrameSubmittedPayload


class TrackSubmitted(_FrozenModel):
    event_type: Literal["TrackSubmitted"] = "TrackSubmitted"
    sequence: int
    phase: str
    visibility: Literal["PRIVATE"] = "PRIVATE"
    actor_player_id: str
    payload: TrackSubmittedPayload


class WatchSubmitted(_FrozenModel):
    event_type: Literal["WatchSubmitted"] = "WatchSubmitted"
    sequence: int
    phase: str
    visibility: Literal["PRIVATE"] = "PRIVATE"
    actor_player_id: str
    payload: WatchSubmittedPayload


class CleanSubmitted(_FrozenModel):
    event_type: Literal["CleanSubmitted"] = "CleanSubmitted"
    sequence: int
    phase: str
    visibility: Literal["PRIVATE"] = "PRIVATE"
    actor_player_id: str
    payload: CleanSubmittedPayload


class NightFeedbackDelivered(_FrozenModel):
    event_type: Literal["NightFeedbackDelivered"] = "NightFeedbackDelivered"
    sequence: int
    phase: str
    visibility: Literal["PRIVATE"] = "PRIVATE"
    actor_player_id: str
    payload: NightFeedbackDeliveredPayload


class ActionTimedOut(_FrozenModel):
    event_type: Literal["ActionTimedOut"] = "ActionTimedOut"
    sequence: int
    phase: str
    visibility: Literal["SYSTEM"] = "SYSTEM"
    actor_player_id: str
    payload: ActionTimedOutPayload


class OutputTruncated(_FrozenModel):
    event_type: Literal["OutputTruncated"] = "OutputTruncated"
    sequence: int
    phase: str
    visibility: Literal["SYSTEM"] = "SYSTEM"
    actor_player_id: str
    payload: OutputTruncatedPayload


class OutputInvalid(_FrozenModel):
    event_type: Literal["OutputInvalid"] = "OutputInvalid"
    sequence: int
    phase: str
    visibility: Literal["SYSTEM"] = "SYSTEM"
    actor_player_id: str
    payload: OutputInvalidPayload


class DayVoteResolved(_FrozenModel):
    event_type: Literal["DayVoteResolved"] = "DayVoteResolved"
    sequence: int
    phase: str
    visibility: Literal["PUBLIC"] = "PUBLIC"
    actor_player_id: str | None = None
    payload: DayVoteResolvedPayload


class NightResolved(_FrozenModel):
    event_type: Literal["NightResolved"] = "NightResolved"
    sequence: int
    phase: str
    visibility: Literal["SYSTEM"] = "SYSTEM"
    actor_player_id: str | None = None
    payload: NightResolvedPayload


class DetectiveResultDelivered(_FrozenModel):
    event_type: Literal["DetectiveResultDelivered"] = "DetectiveResultDelivered"
    sequence: int
    phase: str
    visibility: Literal["PRIVATE"] = "PRIVATE"
    actor_player_id: str
    payload: DetectiveResultDeliveredPayload


class PlayerEliminated(_FrozenModel):
    event_type: Literal["PlayerEliminated"] = "PlayerEliminated"
    sequence: int
    phase: str
    visibility: Literal["PUBLIC"] = "PUBLIC"
    actor_player_id: str | None = None
    payload: PlayerEliminatedPayload


class PhaseResolved(_FrozenModel):
    event_type: Literal["PhaseResolved"] = "PhaseResolved"
    sequence: int
    phase: str
    visibility: Literal["SYSTEM"] = "SYSTEM"
    actor_player_id: str | None = None
    payload: PhaseResolvedPayload


class GameTerminated(_FrozenModel):
    event_type: Literal["GameTerminated"] = "GameTerminated"
    sequence: int
    phase: str
    visibility: Literal["PUBLIC"] = "PUBLIC"
    actor_player_id: str | None = None
    payload: GameTerminatedPayload


class RoleClaimed(_FrozenModel):
    event_type: Literal["RoleClaimed"] = "RoleClaimed"
    sequence: int
    phase: str
    visibility: Literal["PUBLIC"] = "PUBLIC"
    actor_player_id: str
    payload: RoleClaimedPayload


class SeatTakenOver(_FrozenModel):
    event_type: Literal["SeatTakenOver"] = "SeatTakenOver"
    sequence: int
    phase: str
    visibility: Literal["SYSTEM"] = "SYSTEM"
    actor_player_id: str | None = None
    payload: SeatTakenOverPayload


# --------------------------------------------------------------------------- #
# Discriminated union
# --------------------------------------------------------------------------- #

Event = Annotated[
    GameCreated
    | RolesAssigned
    | PhaseStarted
    | PublicMessageSubmitted
    | PrivateMessageSubmitted
    | VoteSubmitted
    | MafiaKillVoteSubmitted
    | ProtectSubmitted
    | InvestigateSubmitted
    | RoleblockSubmitted
    | FrameSubmitted
    | TrackSubmitted
    | WatchSubmitted
    | CleanSubmitted
    | NightFeedbackDelivered
    | ActionTimedOut
    | OutputTruncated
    | OutputInvalid
    | DayVoteResolved
    | NightResolved
    | DetectiveResultDelivered
    | PlayerEliminated
    | PhaseResolved
    | GameTerminated
    | RoleClaimed
    | SeatTakenOver,
    Field(discriminator="event_type"),
]

EventAdapter: TypeAdapter[Event] = TypeAdapter(Event)

EVENT_TYPES: tuple[str, ...] = (
    "GameCreated",
    "RolesAssigned",
    "PhaseStarted",
    "PublicMessageSubmitted",
    "PrivateMessageSubmitted",
    "VoteSubmitted",
    "MafiaKillVoteSubmitted",
    "ProtectSubmitted",
    "InvestigateSubmitted",
    "RoleblockSubmitted",
    "FrameSubmitted",
    "TrackSubmitted",
    "WatchSubmitted",
    "CleanSubmitted",
    "NightFeedbackDelivered",
    "ActionTimedOut",
    "OutputTruncated",
    "OutputInvalid",
    "DayVoteResolved",
    "NightResolved",
    "DetectiveResultDelivered",
    "PlayerEliminated",
    "PhaseResolved",
    "GameTerminated",
    "RoleClaimed",
    "SeatTakenOver",
)


__all__ = [
    "EVENT_TYPES",
    "ActionTimedOut",
    "ActionTimedOutPayload",
    "CleanSubmitted",
    "CleanSubmittedPayload",
    "DayVoteResolved",
    "DayVoteResolvedPayload",
    "DetectiveResultDelivered",
    "DetectiveResultDeliveredPayload",
    "Event",
    "EventAdapter",
    "FrameSubmitted",
    "FrameSubmittedPayload",
    "GameCreated",
    "GameCreatedPayload",
    "GameTerminated",
    "GameTerminatedPayload",
    "InvestigateSubmitted",
    "InvestigateSubmittedPayload",
    "MafiaKillVoteSubmitted",
    "MafiaKillVoteSubmittedPayload",
    "NightFeedbackDelivered",
    "NightFeedbackDeliveredPayload",
    "NightResolved",
    "NightResolvedPayload",
    "OutputInvalid",
    "OutputInvalidPayload",
    "OutputTruncated",
    "OutputTruncatedPayload",
    "PhaseResolved",
    "PhaseResolvedPayload",
    "PhaseStarted",
    "PhaseStartedPayload",
    "PlayerEliminated",
    "PlayerEliminatedPayload",
    "PrivateMessageSubmitted",
    "PrivateMessageSubmittedPayload",
    "ProtectSubmitted",
    "ProtectSubmittedPayload",
    "PublicMessageSubmitted",
    "PublicMessageSubmittedPayload",
    "RoleClaimed",
    "RoleClaimedPayload",
    "RoleblockSubmitted",
    "RoleblockSubmittedPayload",
    "RolesAssigned",
    "RolesAssignedPayload",
    "SeatAssignment",
    "SeatTakenOver",
    "SeatTakenOverPayload",
    "TrackSubmitted",
    "TrackSubmittedPayload",
    "Visibility",
    "VoteSubmitted",
    "VoteSubmittedPayload",
    "WatchSubmitted",
    "WatchSubmittedPayload",
]
