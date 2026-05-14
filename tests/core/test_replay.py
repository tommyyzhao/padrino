"""Tests for the deterministic replay primitives."""

from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from padrino.core.engine.event_log import EventLog
from padrino.core.engine.events import (
    DayVoteResolved,
    DayVoteResolvedPayload,
    DetectiveResultDelivered,
    DetectiveResultDeliveredPayload,
    Event,
    EventAdapter,
    GameCreated,
    GameCreatedPayload,
    GameTerminated,
    GameTerminatedPayload,
    NightResolved,
    NightResolvedPayload,
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
    RolesAssigned,
    RolesAssignedPayload,
    SeatAssignment,
    VoteSubmitted,
    VoteSubmittedPayload,
)
from padrino.core.engine.reducer import apply_event, initial_state
from padrino.core.engine.replay import (
    ReplayHashMismatchError,
    replay_event_log,
    replay_events,
)
from padrino.core.enums import Faction, Role


def _seat_assignments() -> tuple[SeatAssignment, ...]:
    return (
        SeatAssignment(
            public_player_id="P01", seat_index=0, role=Role.MAFIA_GOON, faction=Faction.MAFIA
        ),
        SeatAssignment(
            public_player_id="P02", seat_index=1, role=Role.MAFIA_GOON, faction=Faction.MAFIA
        ),
        SeatAssignment(
            public_player_id="P03", seat_index=2, role=Role.DETECTIVE, faction=Faction.TOWN
        ),
        SeatAssignment(
            public_player_id="P04", seat_index=3, role=Role.DOCTOR, faction=Faction.TOWN
        ),
        SeatAssignment(
            public_player_id="P05", seat_index=4, role=Role.VILLAGER, faction=Faction.TOWN
        ),
        SeatAssignment(
            public_player_id="P06", seat_index=5, role=Role.VILLAGER, faction=Faction.TOWN
        ),
        SeatAssignment(
            public_player_id="P07", seat_index=6, role=Role.VILLAGER, faction=Faction.TOWN
        ),
    )


def _scripted_events() -> list[Event]:
    """A short but exhaustive 7-seat event stream covering every reducer branch."""
    return [
        GameCreated(
            sequence=0,
            phase="SETUP",
            payload=GameCreatedPayload(
                ruleset_id="mini7_v1",
                game_id="g-replay-01",
                game_seed="seed-replay",
                player_count=7,
            ),
        ),
        RolesAssigned(
            sequence=1,
            phase="SETUP",
            payload=RolesAssignedPayload(assignments=_seat_assignments()),
        ),
        PhaseStarted(
            sequence=2,
            phase="DAY_1_DISCUSSION",
            payload=PhaseStartedPayload(phase_kind="DAY_DISCUSSION", day=1, round=1),
        ),
        PublicMessageSubmitted(
            sequence=3,
            phase="DAY_1_DISCUSSION",
            actor_player_id="P05",
            payload=PublicMessageSubmittedPayload(text="hi", round_index=1),
        ),
        PhaseStarted(
            sequence=4,
            phase="DAY_1_VOTE",
            payload=PhaseStartedPayload(phase_kind="DAY_VOTE", day=1, round=0),
        ),
        VoteSubmitted(
            sequence=5,
            phase="DAY_1_VOTE",
            actor_player_id="P05",
            payload=VoteSubmittedPayload(target="P02", is_abstain=False),
        ),
        DayVoteResolved(
            sequence=6,
            phase="DAY_1_VOTE",
            payload=DayVoteResolvedPayload(
                eliminated="P02",
                vote_tally={"P02": 4, "P05": 1},
                reason="UNIQUE_PLURALITY",
            ),
        ),
        PlayerEliminated(
            sequence=7,
            phase="DAY_1_VOTE",
            payload=PlayerEliminatedPayload(
                public_player_id="P02",
                role=Role.MAFIA_GOON,
                faction=Faction.MAFIA,
                cause="DAY_VOTE",
            ),
        ),
        PhaseStarted(
            sequence=8,
            phase="NIGHT_1_MAFIA_DISCUSSION",
            payload=PhaseStartedPayload(phase_kind="NIGHT_MAFIA_DISCUSSION", day=1, round=0),
        ),
        PrivateMessageSubmitted(
            sequence=9,
            phase="NIGHT_1_MAFIA_DISCUSSION",
            actor_player_id="P01",
            payload=PrivateMessageSubmittedPayload(text="kill P05", channel_id="mafia"),
        ),
        PhaseStarted(
            sequence=10,
            phase="NIGHT_1_ACTIONS",
            payload=PhaseStartedPayload(phase_kind="NIGHT_ACTIONS", day=1, round=0),
        ),
        ProtectSubmitted(
            sequence=11,
            phase="NIGHT_1_ACTIONS",
            actor_player_id="P04",
            payload=ProtectSubmittedPayload(target="P05"),
        ),
        NightResolved(
            sequence=12,
            phase="NIGHT_1_ACTIONS",
            payload=NightResolvedPayload(eliminated=None, protected="P05", mafia_kill_target="P05"),
        ),
        DetectiveResultDelivered(
            sequence=13,
            phase="NIGHT_1_ACTIONS",
            actor_player_id="P03",
            payload=DetectiveResultDeliveredPayload(target="P01", finding="MAFIA"),
        ),
        PhaseStarted(
            sequence=14,
            phase="DAY_2_DISCUSSION",
            payload=PhaseStartedPayload(phase_kind="DAY_DISCUSSION", day=2, round=1),
        ),
        GameTerminated(
            sequence=15,
            phase="DAY_2_DISCUSSION",
            payload=GameTerminatedPayload(winner="TOWN", reason="ALL_MAFIA_ELIMINATED"),
        ),
    ]


def test_replay_events_matches_incremental_apply() -> None:
    events = _scripted_events()
    expected = initial_state()
    for ev in events:
        expected = apply_event(expected, ev)
    assert replay_events(events) == expected


def test_replay_events_on_empty_returns_initial_state() -> None:
    assert replay_events([]) == initial_state()


def test_replay_events_terminal_state_fields() -> None:
    final = replay_events(_scripted_events())
    assert final.terminal_result == "TOWN"
    assert final.terminal_reason == "ALL_MAFIA_ELIMINATED"
    assert final.ruleset_id == "mini7_v1"
    assert final.game_id == "g-replay-01"


def test_replay_event_log_reproduces_sequences_and_hashes() -> None:
    original = EventLog()
    for ev in _scripted_events():
        original.append(ev.model_dump(mode="json"))

    replayed = replay_event_log(original.events)

    assert len(replayed.events) == len(original.events)
    for orig, new in zip(original.events, replayed.events, strict=True):
        assert new.sequence == orig.sequence
        assert new.prev_event_hash == orig.prev_event_hash
        assert new.event_hash == orig.event_hash


def test_replay_event_log_validates_via_event_adapter() -> None:
    original = EventLog()
    for ev in _scripted_events():
        original.append(ev.model_dump(mode="json"))

    replayed = replay_event_log(original.events)
    # Every replayed body must still validate against the typed Event schema.
    for stored in replayed.events:
        EventAdapter.validate_python(stored.body)


def test_replay_event_log_tamper_raises_at_correct_sequence() -> None:
    original = EventLog()
    for ev in _scripted_events():
        original.append(ev.model_dump(mode="json"))

    tampered_stored = list(original.events)
    # Mutate the body of event #3 to a different (but still hashable) payload.
    bad_body = deepcopy(tampered_stored[3].body)
    bad_body["payload"]["text"] = "TAMPERED"
    tampered_stored[3] = tampered_stored[3].model_copy(update={"body": bad_body})

    with pytest.raises(ReplayHashMismatchError) as exc_info:
        replay_event_log(tampered_stored)
    assert exc_info.value.sequence == 3


def test_replay_event_log_insensitive_to_created_at() -> None:
    """`created_at` lives inside the body but is excluded from the hash."""
    original = EventLog()
    for ev in _scripted_events():
        body: dict[str, Any] = ev.model_dump(mode="json")
        body["created_at"] = "2026-01-01T00:00:00Z"
        original.append(body)

    # Build a "stored" list where each body uses a different created_at but
    # keeps the original event_hash on the envelope.
    rebuilt = []
    for stored in original.events:
        future_body = dict(stored.body)
        future_body["created_at"] = "2099-12-31T23:59:59Z"
        rebuilt.append(stored.model_copy(update={"body": future_body}))

    # Replay still succeeds because hashes exclude created_at.
    replayed = replay_event_log(rebuilt)
    for orig, new in zip(original.events, replayed.events, strict=True):
        assert new.event_hash == orig.event_hash


def test_replay_event_log_on_empty_returns_empty_log() -> None:
    log = replay_event_log([])
    assert log.events == ()


def test_replay_module_has_no_forbidden_imports() -> None:
    src = Path("src/padrino/core/engine/replay.py").read_text()
    tree = ast.parse(src)
    forbidden = {
        "padrino.db",
        "padrino.llm",
        "padrino.api",
        "padrino.runner",
        "sqlalchemy",
        "litellm",
        "httpx",
        "time",
        "datetime",
        "random",
        "secrets",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in forbidden, f"forbidden import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            assert node.module not in forbidden, f"forbidden from-import: {node.module}"
