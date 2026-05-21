"""Tests for the offline observation-privacy audit (US-078).

``audit_observation_log_for_seat`` mirrors ``assert_ranked_observation_safe``
semantically but collects every finding instead of raising on the first.
These tests pin the audit's contract against hand-crafted observation logs:

* a deliberate role leak in a public event yields exactly one finding,
* the mafia-teammates top-level field is exempt (the walker doesn't descend
  into it because it lives outside the event payloads),
* an empty observation log yields zero findings,
* a hypothesis property test asserts the audit returns no false positives
  for any synthetically-clean observation it can generate.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from padrino.core.engine.event_log import EventLog
from padrino.core.engine.legal_actions import legal_actions_for
from padrino.core.engine.state import GameState, Phase, Seat
from padrino.core.enums import Faction, PhaseKind, Role
from padrino.core.observation_privacy import (
    FORBIDDEN_PAYLOAD_KEYS,
    LeakFinding,
    audit_observation_log_for_seat,
)
from padrino.core.observations import (
    EventEntry,
    MessageLimits,
    Observation,
    YouInfo,
    build_observation,
)
from padrino.core.rulesets import mini7_v1


def _seven_seats() -> tuple[Seat, ...]:
    spec: list[tuple[str, int, Role, Faction]] = [
        ("P01", 0, Role.MAFIA_GOON, Faction.MAFIA),
        ("P02", 1, Role.MAFIA_GOON, Faction.MAFIA),
        ("P03", 2, Role.DETECTIVE, Faction.TOWN),
        ("P04", 3, Role.DOCTOR, Faction.TOWN),
        ("P05", 4, Role.VILLAGER, Faction.TOWN),
        ("P06", 5, Role.VILLAGER, Faction.TOWN),
        ("P07", 6, Role.VILLAGER, Faction.TOWN),
    ]
    return tuple(
        Seat(
            public_player_id=pid,
            seat_index=idx,
            role=role,
            faction=faction,
            alive=True,
            death_phase=None,
            last_protected_target=None,
        )
        for pid, idx, role, faction in spec
    )


def _state() -> GameState:
    return GameState(
        ruleset_id="mini7_v1",
        game_id="G-AUDIT",
        game_seed="seed",
        current_phase=Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=1),
        seats=_seven_seats(),
        day=1,
    )


def _obs_with_public_payload(
    payload: dict[str, object],
    *,
    actor: str | None = "P01",
    private_memory: str = "",
) -> Observation:
    state = _state()
    seat = state.seats[4]
    entry = EventEntry(
        sequence=7,
        phase="DAY_1_DISCUSSION_ROUND_1",
        event_type="TestEvent",
        actor_player_id=actor,
        payload=payload,
    )
    return Observation(
        ruleset_id="mini7_v1",
        game_public_id=state.game_id,
        phase="DAY_1_DISCUSSION_ROUND_1",
        day=1,
        round=1,
        you=YouInfo(
            player_id=seat.public_player_id,
            alive=seat.alive,
            role=seat.role,
            faction=seat.faction,
        ),
        alive_players=tuple(s.public_player_id for s in state.seats),
        dead_players=(),
        public_events=(entry,),
        private_events=(),
        legal_actions=legal_actions_for(state, seat),
        your_private_memory=private_memory,
        message_limits=MessageLimits(
            public_message_max_chars=mini7_v1.PUBLIC_MESSAGE_MAX_CHARS,
            private_message_max_chars=mini7_v1.PRIVATE_MESSAGE_MAX_CHARS,
            memory_update_max_chars=mini7_v1.MEMORY_UPDATE_MAX_CHARS,
        ),
    )


# --------------------------------------------------------------------------- #
# Hand-crafted findings
# --------------------------------------------------------------------------- #


def test_role_leak_in_public_event_yields_exactly_one_finding() -> None:
    obs = _obs_with_public_payload({"target": "P03", "role": "DETECTIVE"}, actor="P01")
    findings = audit_observation_log_for_seat([obs], seat_id="P05")

    assert len(findings) == 1
    finding = findings[0]
    assert finding.field_path == "public_events[seq=7].payload.role"
    assert finding.seat_observed_by == "P05"
    # The actor that produced the leaking event is the canonical "owner".
    assert finding.seat_owning_the_leak == "P01"
    # The raw leaked value is NEVER embedded in the finding — only a redacted
    # type+length shape label. Confirm by checking the literal value never
    # appears in the redacted column.
    assert "DETECTIVE" not in finding.leaked_value_redacted
    assert finding.leaked_value_redacted.startswith("<str:")


def test_mafia_teammates_top_level_is_not_walked() -> None:
    """``mafia_teammates`` is a top-level Observation field; the audit walks
    only event payloads and your_private_memory, so a mafia seat seeing its
    teammates by id is NEVER a finding even though the same key inside a
    payload would be."""
    state = _state()
    obs = build_observation(state, state.seats[0], EventLog(), mini7_v1)
    assert obs.mafia_teammates == ("P02",)

    findings = audit_observation_log_for_seat([obs], seat_id="P01")
    assert findings == []


def test_empty_observation_log_yields_zero_findings() -> None:
    assert audit_observation_log_for_seat([], seat_id="P01") == []


def test_clean_built_observation_yields_zero_findings() -> None:
    state = _state()
    obs = build_observation(state, state.seats[4], EventLog(), mini7_v1)
    assert audit_observation_log_for_seat([obs], seat_id="P05") == []


def test_multiple_distinct_leaks_each_produce_one_finding() -> None:
    obs = _obs_with_public_payload(
        {"agent_build_id": "build-42", "model_id": "glm-4.7", "mu": 25.0},
        actor="P02",
    )
    findings = audit_observation_log_for_seat([obs], seat_id="P05")
    paths = {f.field_path for f in findings}
    assert paths == {
        "public_events[seq=7].payload.agent_build_id",
        "public_events[seq=7].payload.model_id",
        "public_events[seq=7].payload.mu",
    }
    for f in findings:
        assert f.seat_observed_by == "P05"
        assert f.seat_owning_the_leak == "P02"


def test_memory_token_leak_produces_finding_without_value() -> None:
    obs = _obs_with_public_payload(
        {"text": "hello"},
        actor="P01",
        private_memory="The opponent's openskill mu was 30.0",
    )
    findings = audit_observation_log_for_seat([obs], seat_id="P05")
    memory_findings = [f for f in findings if f.field_path == "your_private_memory"]
    assert len(memory_findings) == 1
    finding = memory_findings[0]
    assert finding.seat_observed_by == "P05"
    assert finding.seat_owning_the_leak is None
    # Memory redaction never embeds the raw memory string.
    assert "30.0" not in finding.leaked_value_redacted
    assert finding.leaked_value_redacted.startswith("<str:contains:")


def test_foreign_game_id_in_payload_yields_finding() -> None:
    obs = _obs_with_public_payload({"game_id": "G-other"}, actor="P01")
    findings = audit_observation_log_for_seat([obs], seat_id="P05")
    assert len(findings) == 1
    assert findings[0].field_path == "public_events[seq=7].payload.game_id"


def test_matching_game_id_does_not_leak() -> None:
    obs = _obs_with_public_payload({"game_id": "G-AUDIT"}, actor="P01")
    assert audit_observation_log_for_seat([obs], seat_id="P05") == []


# --------------------------------------------------------------------------- #
# LeakFinding is frozen
# --------------------------------------------------------------------------- #


def test_leak_finding_is_immutable() -> None:
    finding = LeakFinding(
        field_path="public_events[seq=1].payload.role",
        leaked_value_redacted="<str:len=8>",
        seat_observed_by="P05",
        seat_owning_the_leak="P01",
    )
    with pytest.raises(ValidationError):
        finding.field_path = "tampered"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Hypothesis property test — no false positives on synthetically-clean logs
# --------------------------------------------------------------------------- #


_SAFE_KEYS: Sequence[str] = (
    "text",
    "round_index",
    "target",
    "channel_id",
    "vote_tally",
    "reason",
    "is_abstain",
    "phase_kind",
    "day",
    "round",
)
assert set(_SAFE_KEYS).isdisjoint(FORBIDDEN_PAYLOAD_KEYS)

_SAFE_MEMORY_STRATEGY = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126, blacklist_characters="\"'"),
    min_size=0,
    max_size=120,
).filter(
    lambda s: (
        not any(
            token in s.lower()
            for token in ("agent_build_id", "model_id", "gauntlet_clone_index", "openskill")
        )
    )
)

# Safe values: scalars + recursive lists/dicts whose keys are sampled only
# from _SAFE_KEYS, so a generated payload can never accidentally include a
# forbidden key at any nesting depth.
_SAFE_LEAF_STRATEGY = st.one_of(
    st.text(
        alphabet=st.characters(min_codepoint=32, max_codepoint=126, blacklist_characters="\"'"),
        max_size=40,
    ),
    st.integers(min_value=-10, max_value=10),
    st.booleans(),
    st.none(),
)


def _safe_value_strategy() -> st.SearchStrategy[object]:
    return st.recursive(
        _SAFE_LEAF_STRATEGY,
        lambda children: st.one_of(
            st.lists(children, max_size=4),
            st.dictionaries(st.sampled_from(_SAFE_KEYS), children, max_size=4),
        ),
        max_leaves=8,
    )


@st.composite
def _safe_payload(draw: st.DrawFn) -> dict[str, object]:
    return draw(st.dictionaries(st.sampled_from(_SAFE_KEYS), _safe_value_strategy(), max_size=4))


@st.composite
def _clean_observation(draw: st.DrawFn) -> Observation:
    state = _state()
    seat = state.seats[draw(st.integers(min_value=0, max_value=6))]
    payloads = draw(st.lists(_safe_payload(), min_size=0, max_size=4))
    entries = tuple(
        EventEntry(
            sequence=i,
            phase="DAY_1_DISCUSSION_ROUND_1",
            event_type="TestEvent",
            actor_player_id=state.seats[i % 7].public_player_id,
            payload=payload,
        )
        for i, payload in enumerate(payloads)
    )
    memory = draw(_SAFE_MEMORY_STRATEGY)
    return Observation(
        ruleset_id="mini7_v1",
        game_public_id=state.game_id,
        phase="DAY_1_DISCUSSION_ROUND_1",
        day=1,
        round=1,
        you=YouInfo(
            player_id=seat.public_player_id,
            alive=seat.alive,
            role=seat.role,
            faction=seat.faction,
        ),
        alive_players=tuple(s.public_player_id for s in state.seats),
        dead_players=(),
        public_events=entries,
        private_events=(),
        legal_actions=legal_actions_for(state, seat),
        your_private_memory=memory,
        message_limits=MessageLimits(
            public_message_max_chars=mini7_v1.PUBLIC_MESSAGE_MAX_CHARS,
            private_message_max_chars=mini7_v1.PRIVATE_MESSAGE_MAX_CHARS,
            memory_update_max_chars=mini7_v1.MEMORY_UPDATE_MAX_CHARS,
        ),
    )


@settings(
    max_examples=75,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(obs=_clean_observation())
def test_audit_returns_no_findings_for_clean_observations(obs: Observation) -> None:
    findings = audit_observation_log_for_seat([obs], seat_id=obs.you.player_id)
    assert findings == [], [(f.field_path, f.leaked_value_redacted) for f in findings]
