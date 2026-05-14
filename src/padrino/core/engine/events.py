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

from padrino.core.enums import Faction, Role

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


class PrivateMessageSubmittedPayload(_FrozenModel):
    text: str
    channel_id: str


class VoteSubmittedPayload(_FrozenModel):
    target: str | None
    is_abstain: bool


class MafiaKillVoteSubmittedPayload(_FrozenModel):
    target: str | None


class ProtectSubmittedPayload(_FrozenModel):
    target: str | None


class InvestigateSubmittedPayload(_FrozenModel):
    target: str | None


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


class DetectiveResultDeliveredPayload(_FrozenModel):
    target: str
    finding: Literal["MAFIA", "TOWN"]


class PlayerEliminatedPayload(_FrozenModel):
    public_player_id: str
    role: Role
    faction: Faction
    cause: str


class PhaseResolvedPayload(_FrozenModel):
    resolved_phase: str


class GameTerminatedPayload(_FrozenModel):
    winner: Literal["TOWN", "MAFIA", "DRAW"]
    reason: str


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
    | ActionTimedOut
    | OutputTruncated
    | OutputInvalid
    | DayVoteResolved
    | NightResolved
    | DetectiveResultDelivered
    | PlayerEliminated
    | PhaseResolved
    | GameTerminated,
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
    "ActionTimedOut",
    "OutputTruncated",
    "OutputInvalid",
    "DayVoteResolved",
    "NightResolved",
    "DetectiveResultDelivered",
    "PlayerEliminated",
    "PhaseResolved",
    "GameTerminated",
)


__all__ = [
    "EVENT_TYPES",
    "ActionTimedOut",
    "ActionTimedOutPayload",
    "DayVoteResolved",
    "DayVoteResolvedPayload",
    "DetectiveResultDelivered",
    "DetectiveResultDeliveredPayload",
    "Event",
    "EventAdapter",
    "GameCreated",
    "GameCreatedPayload",
    "GameTerminated",
    "GameTerminatedPayload",
    "InvestigateSubmitted",
    "InvestigateSubmittedPayload",
    "MafiaKillVoteSubmitted",
    "MafiaKillVoteSubmittedPayload",
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
    "RolesAssigned",
    "RolesAssignedPayload",
    "SeatAssignment",
    "Visibility",
    "VoteSubmitted",
    "VoteSubmittedPayload",
]
