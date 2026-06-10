"""Tests for the broadcaster plan function (US-088)."""

from __future__ import annotations

from padrino.public.broadcaster import (
    CadenceConfig,
    plan_broadcast,
)

_CADENCE = CadenceConfig()


def _event(event_type: str, seq: int, visibility: str = "PUBLIC") -> dict[str, object]:
    return {
        "event_type": event_type,
        "sequence": seq,
        "phase": "day_1",
        "visibility": visibility,
        "actor_player_id": None,
        "payload": {},
        "prev_event_hash": "abc123",
        "event_hash": "def456",
    }


# ---------------------------------------------------------------------------
# Frame count == projected-event count
# ---------------------------------------------------------------------------


def test_all_public_events_become_frames() -> None:
    events = [
        _event("PhaseStarted", 1),
        _event("PublicMessageSubmitted", 2),
        _event("VoteSubmitted", 3),
    ]
    frames = plan_broadcast(events, _CADENCE)
    assert len(frames) == 3


def test_private_events_are_dropped() -> None:
    events = [
        _event("PhaseStarted", 1),
        _event("PrivateMessageSubmitted", 2, visibility="PRIVATE"),
        _event("PublicMessageSubmitted", 3),
    ]
    frames = plan_broadcast(events, _CADENCE)
    assert len(frames) == 2


def test_system_events_are_dropped() -> None:
    events = [
        _event("RolesAssigned", 1, visibility="SYSTEM"),
        _event("PhaseStarted", 2),
    ]
    frames = plan_broadcast(events, _CADENCE)
    assert len(frames) == 1


def test_empty_input_returns_empty_plan() -> None:
    assert plan_broadcast([], _CADENCE) == []


def test_all_non_public_returns_empty_plan() -> None:
    events = [
        _event("RolesAssigned", 1, visibility="SYSTEM"),
        _event("PrivateMessageSubmitted", 2, visibility="PRIVATE"),
    ]
    assert plan_broadcast(events, _CADENCE) == []


def test_mixed_visibility_frame_count() -> None:
    events = [
        _event("PhaseStarted", 1),
        _event("PrivateMessageSubmitted", 2, visibility="PRIVATE"),
        _event("RolesAssigned", 3, visibility="SYSTEM"),
        _event("PublicMessageSubmitted", 4),
        _event("DayVoteResolved", 5),
    ]
    frames = plan_broadcast(events, _CADENCE)
    assert len(frames) == 3


# ---------------------------------------------------------------------------
# Ordering preserved
# ---------------------------------------------------------------------------


def test_frame_order_matches_input_order() -> None:
    events = [
        _event("PhaseStarted", 1),
        _event("PublicMessageSubmitted", 2),
        _event("VoteSubmitted", 3),
        _event("PlayerEliminated", 4),
        _event("GameTerminated", 5),
    ]
    frames = plan_broadcast(events, _CADENCE)
    sequences = [f.event["sequence"] for f in frames]
    assert sequences == [1, 2, 3, 4, 5]


def test_mixed_visibility_preserves_public_order() -> None:
    events = [
        _event("PhaseStarted", 1),
        _event("PrivateMessageSubmitted", 2, visibility="PRIVATE"),
        _event("PublicMessageSubmitted", 3),
        _event("RolesAssigned", 4, visibility="SYSTEM"),
        _event("VoteSubmitted", 5),
    ]
    frames = plan_broadcast(events, _CADENCE)
    assert [f.event["sequence"] for f in frames] == [1, 3, 5]


# ---------------------------------------------------------------------------
# Delays match config for each event type
# ---------------------------------------------------------------------------


def test_chat_event_uses_chat_delay() -> None:
    cadence = CadenceConfig(chat_ms=9_000, default_ms=1)
    frames = plan_broadcast([_event("PublicMessageSubmitted", 1)], cadence)
    assert frames[0].delay_ms == 9_000


def test_phase_started_uses_phase_delay() -> None:
    cadence = CadenceConfig(phase_ms=8_000, default_ms=1)
    frames = plan_broadcast([_event("PhaseStarted", 1)], cadence)
    assert frames[0].delay_ms == 8_000


def test_phase_resolved_uses_phase_delay() -> None:
    cadence = CadenceConfig(phase_ms=8_000, default_ms=1)
    frames = plan_broadcast([_event("PhaseResolved", 1)], cadence)
    assert frames[0].delay_ms == 8_000


def test_player_eliminated_uses_elimination_delay() -> None:
    cadence = CadenceConfig(elimination_ms=7_000, default_ms=1)
    frames = plan_broadcast([_event("PlayerEliminated", 1)], cadence)
    assert frames[0].delay_ms == 7_000


def test_day_vote_resolved_uses_resolution_delay() -> None:
    cadence = CadenceConfig(resolution_ms=6_000, default_ms=1)
    frames = plan_broadcast([_event("DayVoteResolved", 1)], cadence)
    assert frames[0].delay_ms == 6_000


def test_night_resolved_uses_resolution_delay() -> None:
    cadence = CadenceConfig(resolution_ms=6_000, default_ms=1)
    frames = plan_broadcast([_event("NightResolved", 1)], cadence)
    assert frames[0].delay_ms == 6_000


def test_game_created_uses_default_delay() -> None:
    cadence = CadenceConfig(default_ms=5_555)
    frames = plan_broadcast([_event("GameCreated", 1)], cadence)
    assert frames[0].delay_ms == 5_555


def test_vote_submitted_uses_default_delay() -> None:
    cadence = CadenceConfig(default_ms=5_555)
    frames = plan_broadcast([_event("VoteSubmitted", 1)], cadence)
    assert frames[0].delay_ms == 5_555


def test_game_terminated_uses_default_delay() -> None:
    cadence = CadenceConfig(default_ms=5_555)
    frames = plan_broadcast([_event("GameTerminated", 1)], cadence)
    assert frames[0].delay_ms == 5_555


def test_mixed_events_get_correct_delays() -> None:
    cadence = CadenceConfig(
        chat_ms=2_500,
        phase_ms=3_000,
        elimination_ms=4_000,
        resolution_ms=3_500,
        default_ms=1_500,
    )
    events = [
        _event("PublicMessageSubmitted", 1),
        _event("PhaseStarted", 2),
        _event("PlayerEliminated", 3),
        _event("DayVoteResolved", 4),
        _event("VoteSubmitted", 5),
    ]
    frames = plan_broadcast(events, cadence)
    assert frames[0].delay_ms == 2_500
    assert frames[1].delay_ms == 3_000
    assert frames[2].delay_ms == 4_000
    assert frames[3].delay_ms == 3_500
    assert frames[4].delay_ms == 1_500


# ---------------------------------------------------------------------------
# Frame contents
# ---------------------------------------------------------------------------


def test_frames_carry_public_event_v1_schema_version() -> None:
    frames = plan_broadcast([_event("PhaseStarted", 1)], _CADENCE)
    assert frames[0].event["schema_version"] == "public_event_v1"


def test_frame_event_payload_is_preserved() -> None:
    event = _event("PublicMessageSubmitted", 1)
    event["payload"] = {"message": "Hello town"}
    frames = plan_broadcast([event], _CADENCE)
    assert frames[0].event["payload"] == {"message": "Hello town"}


def test_frame_event_has_correct_event_type() -> None:
    frames = plan_broadcast([_event("PhaseStarted", 42)], _CADENCE)
    assert frames[0].event["event_type"] == "PhaseStarted"
    assert frames[0].event["sequence"] == 42
