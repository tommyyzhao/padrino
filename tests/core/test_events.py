"""Tests for the typed event catalog."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from padrino.core.engine.canonical_json import canonical_dumps
from padrino.core.engine.events import (
    EVENT_TYPES,
    ActionTimedOut,
    ActionTimedOutPayload,
    CleanSubmitted,
    CleanSubmittedPayload,
    DayVoteResolved,
    DayVoteResolvedPayload,
    DetectiveResultDelivered,
    DetectiveResultDeliveredPayload,
    Event,
    EventAdapter,
    FrameSubmitted,
    FrameSubmittedPayload,
    GameCreated,
    GameCreatedPayload,
    GameTerminated,
    GameTerminatedPayload,
    InvestigateSubmitted,
    InvestigateSubmittedPayload,
    MafiaKillVoteSubmitted,
    MafiaKillVoteSubmittedPayload,
    NightFeedbackDelivered,
    NightFeedbackDeliveredPayload,
    NightResolved,
    NightResolvedPayload,
    OutputInvalid,
    OutputInvalidPayload,
    OutputTruncated,
    OutputTruncatedPayload,
    PhaseResolved,
    PhaseResolvedPayload,
    PhaseStarted,
    PhaseStartedPayload,
    PlayerEliminated,
    PlayerEliminatedPayload,
    PrivateMessageSubmitted,
    PrivateMessageSubmittedPayload,
    ProtectSubmitted,
    ProtectSubmittedPayload,
    PublicMessageSubmitted,
    PublicMessageSubmittedPayload,
    RoleblockSubmitted,
    RoleblockSubmittedPayload,
    RoleClaimed,
    RoleClaimedPayload,
    RolesAssigned,
    RolesAssignedPayload,
    SeatAssignment,
    SeatTakenOver,
    SeatTakenOverPayload,
    TrackSubmitted,
    TrackSubmittedPayload,
    VoteSubmitted,
    VoteSubmittedPayload,
    WatchSubmitted,
    WatchSubmittedPayload,
)
from padrino.core.enums import Faction, Role


def _all_events() -> list[Event]:
    """One representative instance for every event type."""
    return [
        GameCreated(
            sequence=0,
            phase="SETUP",
            actor_player_id=None,
            payload=GameCreatedPayload(
                ruleset_id="mini7_v1",
                game_id="game_abc",
                game_seed="0" * 64,
                player_count=7,
            ),
        ),
        RolesAssigned(
            sequence=1,
            phase="SETUP",
            actor_player_id=None,
            payload=RolesAssignedPayload(
                assignments=(
                    SeatAssignment(
                        public_player_id="P01",
                        seat_index=0,
                        role=Role.MAFIA_GOON,
                        faction=Faction.MAFIA,
                    ),
                    SeatAssignment(
                        public_player_id="P02",
                        seat_index=1,
                        role=Role.DOCTOR,
                        faction=Faction.TOWN,
                    ),
                ),
            ),
        ),
        PhaseStarted(
            sequence=2,
            phase="DAY_1_DISCUSSION_R1",
            actor_player_id=None,
            payload=PhaseStartedPayload(
                phase_kind="DAY_DISCUSSION",
                day=1,
                round=1,
            ),
        ),
        PublicMessageSubmitted(
            sequence=3,
            phase="DAY_1_DISCUSSION_R1",
            actor_player_id="P03",
            payload=PublicMessageSubmittedPayload(text="hi all", round_index=1),
        ),
        PrivateMessageSubmitted(
            sequence=4,
            phase="NIGHT_1_MAFIA_DISCUSSION",
            actor_player_id="P01",
            payload=PrivateMessageSubmittedPayload(text="we kill P05", channel_id="mafia"),
        ),
        VoteSubmitted(
            sequence=5,
            phase="DAY_1_VOTE",
            actor_player_id="P02",
            payload=VoteSubmittedPayload(target="P05", is_abstain=False),
        ),
        MafiaKillVoteSubmitted(
            sequence=6,
            phase="NIGHT_1_ACTIONS",
            actor_player_id="P01",
            payload=MafiaKillVoteSubmittedPayload(target="P05"),
        ),
        ProtectSubmitted(
            sequence=7,
            phase="NIGHT_1_ACTIONS",
            actor_player_id="P02",
            payload=ProtectSubmittedPayload(target="P05"),
        ),
        InvestigateSubmitted(
            sequence=8,
            phase="NIGHT_1_ACTIONS",
            actor_player_id="P04",
            payload=InvestigateSubmittedPayload(target="P01"),
        ),
        RoleblockSubmitted(
            sequence=20,
            phase="NIGHT_1_ACTIONS",
            actor_player_id="P01",
            payload=RoleblockSubmittedPayload(target="P04"),
        ),
        FrameSubmitted(
            sequence=21,
            phase="NIGHT_1_ACTIONS",
            actor_player_id="P02",
            payload=FrameSubmittedPayload(target="P05"),
        ),
        TrackSubmitted(
            sequence=22,
            phase="NIGHT_1_ACTIONS",
            actor_player_id="P04",
            payload=TrackSubmittedPayload(target="P01"),
        ),
        WatchSubmitted(
            sequence=23,
            phase="NIGHT_1_ACTIONS",
            actor_player_id="P06",
            payload=WatchSubmittedPayload(target="P05"),
        ),
        CleanSubmitted(
            sequence=24,
            phase="NIGHT_1_ACTIONS",
            actor_player_id="P02",
            payload=CleanSubmittedPayload(target="P05"),
        ),
        NightFeedbackDelivered(
            sequence=25,
            phase="NIGHT_1_ACTIONS",
            actor_player_id="P04",
            payload=NightFeedbackDeliveredPayload(
                code="WATCH_RESULT",
                target="P05",
                visitor_player_ids=("P01", "P02"),
            ),
        ),
        ActionTimedOut(
            sequence=9,
            phase="DAY_1_VOTE",
            actor_player_id="P06",
            payload=ActionTimedOutPayload(
                expected_action_type="VOTE",
                defaulted_to="ABSTAIN",
            ),
        ),
        OutputTruncated(
            sequence=10,
            phase="DAY_1_DISCUSSION_R1",
            actor_player_id="P07",
            payload=OutputTruncatedPayload(reason="byte_limit", raw_byte_length=4096),
        ),
        OutputInvalid(
            sequence=11,
            phase="DAY_1_VOTE",
            actor_player_id="P07",
            payload=OutputInvalidPayload(
                reason="schema_violation",
                validation_errors=("missing field: action",),
            ),
        ),
        DayVoteResolved(
            sequence=12,
            phase="DAY_1_VOTE",
            actor_player_id=None,
            payload=DayVoteResolvedPayload(
                eliminated="P05",
                vote_tally={"P05": 4, "P02": 1, "ABSTAIN": 2},
                reason="UNIQUE_PLURALITY",
            ),
        ),
        NightResolved(
            sequence=13,
            phase="NIGHT_1_ACTIONS",
            actor_player_id=None,
            payload=NightResolvedPayload(
                eliminated="P03",
                protected="P05",
                mafia_kill_target="P03",
            ),
        ),
        DetectiveResultDelivered(
            sequence=14,
            phase="DAY_2_DISCUSSION_R1",
            actor_player_id="P04",
            payload=DetectiveResultDeliveredPayload(target="P01", finding="MAFIA"),
        ),
        PlayerEliminated(
            sequence=15,
            phase="NIGHT_1_ACTIONS",
            actor_player_id=None,
            payload=PlayerEliminatedPayload(
                public_player_id="P03",
                role=Role.VILLAGER,
                faction=Faction.TOWN,
                cause="NIGHT_KILL",
            ),
        ),
        PhaseResolved(
            sequence=16,
            phase="DAY_1_VOTE",
            actor_player_id=None,
            payload=PhaseResolvedPayload(resolved_phase="DAY_1_VOTE"),
        ),
        GameTerminated(
            sequence=17,
            phase="TERMINAL",
            actor_player_id=None,
            payload=GameTerminatedPayload(winner="TOWN", reason="ALL_MAFIA_ELIMINATED"),
        ),
        RoleClaimed(
            sequence=18,
            phase="DAY_1_DISCUSSION_R1",
            actor_player_id="P03",
            payload=RoleClaimedPayload(claimed_role="DETECTIVE"),
        ),
        SeatTakenOver(
            sequence=19,
            phase="DAY_1_DISCUSSION_R1",
            actor_player_id=None,
            payload=SeatTakenOverPayload(
                public_player_id="P03",
                day=1,
                phase="DAY_1_DISCUSSION_R1",
                reason="DISCONNECT_GRACE_EXPIRED",
                replacement_agent_build_ref="agent_build_curated_42",
            ),
        ),
    ]


def test_event_types_catalog_covers_all_classes() -> None:
    expected = {
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
    }
    assert set(EVENT_TYPES) == expected


@pytest.mark.parametrize("event", _all_events(), ids=lambda e: e.event_type)
def test_round_trip_through_canonical_json(event: Event) -> None:
    dumped: dict[str, Any] = event.model_dump(mode="json")
    blob = canonical_dumps(dumped)
    restored_dict = json.loads(blob.decode("utf-8"))
    restored = EventAdapter.validate_python(restored_dict)
    assert restored == event
    # Also confirm round-trip stays canonical.
    assert canonical_dumps(restored.model_dump(mode="json")) == blob


def test_every_event_class_has_a_round_trip_sample() -> None:
    sampled = {type(e).__name__ for e in _all_events()}
    assert sampled == set(EVENT_TYPES)


def test_cleaned_player_eliminated_payload_may_omit_role_and_faction() -> None:
    event = EventAdapter.validate_python(
        {
            "event_type": "PlayerEliminated",
            "sequence": 99,
            "phase": "NIGHT_1_ACTIONS",
            "visibility": "PUBLIC",
            "actor_player_id": None,
            "payload": {
                "public_player_id": "P05",
                "cause": "night_kill",
            },
        }
    )

    assert isinstance(event, PlayerEliminated)
    assert event.payload.public_player_id == "P05"
    assert event.payload.role is None
    assert event.payload.faction is None


def test_events_are_frozen() -> None:
    event = _all_events()[0]
    with pytest.raises(ValidationError):
        event.sequence = 999  # type: ignore[misc]


def test_payloads_are_frozen() -> None:
    payload = GameCreatedPayload(
        ruleset_id="mini7_v1", game_id="game_abc", game_seed="0" * 64, player_count=7
    )
    with pytest.raises(ValidationError):
        payload.player_count = 8  # type: ignore[misc]


def test_event_type_literal_pins_class() -> None:
    # Mismatched event_type literal must fail validation.
    with pytest.raises(ValidationError):
        GameCreated.model_validate(
            {
                "event_type": "RolesAssigned",
                "sequence": 0,
                "phase": "SETUP",
                "visibility": "SYSTEM",
                "actor_player_id": None,
                "payload": {
                    "ruleset_id": "mini7_v1",
                    "game_id": "g",
                    "game_seed": "0" * 64,
                    "player_count": 7,
                },
            }
        )


def test_visibility_literal_pins_class() -> None:
    # PublicMessageSubmitted is always PUBLIC; cannot mark it PRIVATE.
    with pytest.raises(ValidationError):
        PublicMessageSubmitted.model_validate(
            {
                "event_type": "PublicMessageSubmitted",
                "sequence": 0,
                "phase": "DAY_1_DISCUSSION_R1",
                "visibility": "PRIVATE",
                "actor_player_id": "P01",
                "payload": {"text": "hello", "round_index": 1},
            }
        )


def test_event_adapter_discriminates_by_event_type() -> None:
    raw = {
        "event_type": "VoteSubmitted",
        "sequence": 5,
        "phase": "DAY_1_VOTE",
        "visibility": "PUBLIC",
        "actor_player_id": "P02",
        "payload": {"target": "P05", "is_abstain": False},
    }
    parsed = EventAdapter.validate_python(raw)
    assert isinstance(parsed, VoteSubmitted)
    assert parsed.payload.target == "P05"


def test_event_adapter_rejects_unknown_event_type() -> None:
    with pytest.raises(ValidationError):
        EventAdapter.validate_python(
            {
                "event_type": "NotARealEvent",
                "sequence": 0,
                "phase": "SETUP",
                "visibility": "SYSTEM",
                "actor_player_id": None,
                "payload": {},
            }
        )


def test_payload_strict_no_any_string_for_int() -> None:
    # sequence is int and the inputs are validated strictly enough to reject str.
    raw = {
        "event_type": "GameCreated",
        "sequence": "not-an-int",
        "phase": "SETUP",
        "visibility": "SYSTEM",
        "actor_player_id": None,
        "payload": {
            "ruleset_id": "mini7_v1",
            "game_id": "g",
            "game_seed": "0" * 64,
            "player_count": 7,
        },
    }
    with pytest.raises(ValidationError):
        EventAdapter.validate_python(raw)


def test_chat_event_payload_does_not_carry_action_fields() -> None:
    # Mechanical fields must never appear on chat payloads.
    public_fields = set(PublicMessageSubmittedPayload.model_fields)
    private_fields = set(PrivateMessageSubmittedPayload.model_fields)
    forbidden = {"action", "type", "target", "vote", "kill", "protect"}
    assert public_fields & forbidden == set()
    assert private_fields & forbidden == set()


def test_canonical_dump_is_byte_stable_across_two_serializations() -> None:
    event = _all_events()[12]  # DayVoteResolved
    blob_a = canonical_dumps(event.model_dump(mode="json"))
    blob_b = canonical_dumps(event.model_dump(mode="json"))
    assert blob_a == blob_b


def test_seat_assignment_round_trip() -> None:
    seat = SeatAssignment(
        public_player_id="P01",
        seat_index=0,
        role=Role.DETECTIVE,
        faction=Faction.TOWN,
    )
    dumped = seat.model_dump(mode="json")
    blob = canonical_dumps(dumped)
    restored = SeatAssignment.model_validate(json.loads(blob.decode("utf-8")))
    assert restored == seat
