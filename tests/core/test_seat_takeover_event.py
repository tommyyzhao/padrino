"""Tests for the SeatTakenOver pure-core event (US-122).

Covers:
- the event round-trips through the discriminated union / canonical JSON,
- the reducer folds it as provenance-only (no mechanics mutation),
- ``compute_seat_provenance`` derives HUMAN / AI / HUMAN_THEN_AI from the log,
- folding a log containing the event reproduces identical state and the hash
  chain stays valid.
"""

from __future__ import annotations

import json
from itertools import pairwise

from padrino.core.engine.canonical_json import canonical_dumps
from padrino.core.engine.event_log import EventLog
from padrino.core.engine.events import (
    EVENT_TYPES,
    Event,
    EventAdapter,
    GameCreated,
    GameCreatedPayload,
    PhaseStarted,
    PhaseStartedPayload,
    RolesAssigned,
    RolesAssignedPayload,
    SeatAssignment,
    SeatTakenOver,
    SeatTakenOverPayload,
)
from padrino.core.engine.hashing import GENESIS_HASH
from padrino.core.engine.reducer import apply_event, compute_seat_provenance, initial_state
from padrino.core.engine.state import GameState
from padrino.core.enums import Faction, PhaseKind, Role, SeatKind


def _assignments() -> tuple[SeatAssignment, ...]:
    return (
        SeatAssignment(
            public_player_id="P01",
            seat_index=0,
            role=Role.MAFIA_GOON,
            faction=Faction.MAFIA,
            seat_kind=SeatKind.HUMAN,
        ),
        SeatAssignment(
            public_player_id="P02",
            seat_index=1,
            role=Role.DOCTOR,
            faction=Faction.TOWN,
            seat_kind=SeatKind.AI,
        ),
        SeatAssignment(
            public_player_id="P03",
            seat_index=2,
            role=Role.VILLAGER,
            faction=Faction.TOWN,
        ),
    )


def _takeover() -> SeatTakenOver:
    return SeatTakenOver(
        sequence=3,
        phase="DAY_1_DISCUSSION_R1",
        payload=SeatTakenOverPayload(
            public_player_id="P01",
            day=1,
            phase="DAY_1_DISCUSSION_R1",
            reason="DISCONNECT_GRACE_EXPIRED",
            replacement_agent_build_ref="agent_build_curated_42",
        ),
    )


def test_seat_takeover_is_in_catalog() -> None:
    assert "SeatTakenOver" in EVENT_TYPES


def test_seat_takeover_round_trips_through_adapter() -> None:
    event = _takeover()
    dumped = event.model_dump(mode="json")
    blob = canonical_dumps(dumped)
    restored = EventAdapter.validate_python(json.loads(blob.decode("utf-8")))
    assert isinstance(restored, SeatTakenOver)
    assert restored == event
    assert restored.visibility == "SYSTEM"


def test_takeover_payload_has_no_wallclock_or_random_fields() -> None:
    fields = set(SeatTakenOverPayload.model_fields)
    assert fields == {
        "public_player_id",
        "day",
        "phase",
        "reason",
        "replacement_agent_build_ref",
    }


def test_fold_is_state_preserving() -> None:
    created = GameCreated(
        sequence=0,
        phase="SETUP",
        payload=GameCreatedPayload(
            ruleset_id="mini7_v1", game_id="g", game_seed="0" * 64, player_count=3
        ),
    )
    roles = RolesAssigned(
        sequence=1, phase="SETUP", payload=RolesAssignedPayload(assignments=_assignments())
    )
    phase = PhaseStarted(
        sequence=2,
        phase="DAY_1_DISCUSSION_R1",
        payload=PhaseStartedPayload(phase_kind=PhaseKind.DAY_DISCUSSION.value, day=1, round=1),
    )
    takeover = _takeover()

    # State without the takeover.
    state_no_takeover = initial_state()
    base_events: list[Event] = [created, roles, phase]
    for ev in base_events:
        state_no_takeover = apply_event(state_no_takeover, ev)

    # State with the takeover folded in.
    state_with_takeover = initial_state()
    for ev in [*base_events, takeover]:
        state_with_takeover = apply_event(state_with_takeover, ev)

    # Provenance-only: mechanics (seats, phase, day, terminal) are identical.
    assert state_with_takeover == state_no_takeover


def test_compute_seat_provenance_derives_kinds() -> None:
    roles = RolesAssigned(
        sequence=1, phase="SETUP", payload=RolesAssignedPayload(assignments=_assignments())
    )
    log: list[Event] = [roles, _takeover()]
    provenance = compute_seat_provenance(log)
    assert provenance == {
        "P01": "HUMAN_THEN_AI",
        "P02": "AI",
        "P03": "AI",
    }


def test_compute_seat_provenance_human_without_takeover_stays_human() -> None:
    roles = RolesAssigned(
        sequence=1, phase="SETUP", payload=RolesAssignedPayload(assignments=_assignments())
    )
    log: list[Event] = [roles]
    provenance = compute_seat_provenance(log)
    assert provenance["P01"] == "HUMAN"
    assert provenance["P02"] == "AI"


def test_compute_seat_provenance_legacy_log_all_ai() -> None:
    legacy = RolesAssigned(
        sequence=1,
        phase="SETUP",
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
                    role=Role.VILLAGER,
                    faction=Faction.TOWN,
                ),
            )
        ),
    )
    log: list[Event] = [legacy]
    assert compute_seat_provenance(log) == {"P01": "AI", "P02": "AI"}


def test_takeover_keeps_hash_chain_valid() -> None:
    log = EventLog()
    events: list[Event] = [
        GameCreated(
            sequence=0,
            phase="SETUP",
            payload=GameCreatedPayload(
                ruleset_id="mini7_v1", game_id="g", game_seed="0" * 64, player_count=3
            ),
        ),
        RolesAssigned(
            sequence=1, phase="SETUP", payload=RolesAssignedPayload(assignments=_assignments())
        ),
        PhaseStarted(
            sequence=2,
            phase="DAY_1_DISCUSSION_R1",
            payload=PhaseStartedPayload(phase_kind=PhaseKind.DAY_DISCUSSION.value, day=1, round=1),
        ),
        _takeover(),
    ]
    for ev in events:
        log.append(ev.model_dump(mode="json"))

    stored = log.events
    # Contiguous sequences and a chain rooted at GENESIS.
    assert [s.sequence for s in stored] == [0, 1, 2, 3]
    assert stored[0].prev_event_hash == GENESIS_HASH
    for prev, cur in pairwise(stored):
        assert cur.prev_event_hash == prev.event_hash

    # Replaying the stored bodies reproduces a valid state with the takeover folded.
    state = initial_state()
    parsed = [EventAdapter.validate_python(s.body) for s in stored]
    for ev in parsed:
        state = apply_event(state, ev)
    assert isinstance(state, GameState)
    assert compute_seat_provenance(parsed)["P01"] == "HUMAN_THEN_AI"
