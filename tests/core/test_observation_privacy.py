"""Tests for the ranked-mode observation privacy guard."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from padrino.core.engine.event_log import EventLog
from padrino.core.engine.legal_actions import legal_actions_for
from padrino.core.engine.state import GameState, Phase, Seat
from padrino.core.enums import Faction, PhaseKind, Role
from padrino.core.observation_privacy import (
    FORBIDDEN_MEMORY_TOKENS,
    FORBIDDEN_PAYLOAD_KEYS,
    RankedPrivacyViolation,
    assert_ranked_observation_safe,
)
from padrino.core.observations import (
    EventEntry,
    MessageLimits,
    Observation,
    YouInfo,
    build_observation,
)
from padrino.core.rulesets import mini7_v1

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


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
        game_id="G-privacy",
        game_seed="seed",
        current_phase=Phase(kind=PhaseKind.DAY_DISCUSSION, day=1, round=1),
        seats=_seven_seats(),
        day=1,
    )


def _clean_obs() -> Observation:
    """Observation built end-to-end with no private/public events."""
    state = _state()
    return build_observation(state, state.seats[4], EventLog(), mini7_v1)


def _obs_with_payload(payload: dict[str, object], *, in_private: bool = False) -> Observation:
    """Hand-construct an Observation whose only event carries ``payload``.

    Bypasses :func:`build_observation` so a test can stuff arbitrary payloads
    into a single event entry without having to thread a fake event type
    through the typed Event union.
    """
    state = _state()
    seat = state.seats[4]
    entry = EventEntry(
        sequence=1,
        phase="DAY_1_DISCUSSION_ROUND_1",
        event_type="TestEvent",
        actor_player_id=seat.public_player_id,
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
        public_events=() if in_private else (entry,),
        private_events=(entry,) if in_private else (),
        legal_actions=legal_actions_for(state, seat),
        your_private_memory="",
        message_limits=MessageLimits(
            public_message_max_chars=mini7_v1.PUBLIC_MESSAGE_MAX_CHARS,
            private_message_max_chars=mini7_v1.PRIVATE_MESSAGE_MAX_CHARS,
            memory_update_max_chars=mini7_v1.MEMORY_UPDATE_MAX_CHARS,
        ),
    )


# --------------------------------------------------------------------------- #
# Clean observations pass
# --------------------------------------------------------------------------- #


def test_clean_built_observation_passes() -> None:
    assert_ranked_observation_safe(_clean_obs())


def test_clean_observation_with_typical_payloads_passes() -> None:
    obs = _obs_with_payload({"text": "hello world", "round_index": 1})
    assert_ranked_observation_safe(obs)


def test_detective_finding_payload_is_allowed() -> None:
    # `finding` reveals faction-equivalent info legitimately to the detective;
    # the privacy guard must NOT flag it.
    obs = _obs_with_payload({"target": "P01", "finding": "MAFIA"}, in_private=True)
    assert_ranked_observation_safe(obs)


def test_mafia_teammates_top_level_is_allowed() -> None:
    state = _state()
    obs = build_observation(state, state.seats[0], EventLog(), mini7_v1)
    assert obs.mafia_teammates == ("P02",)
    assert_ranked_observation_safe(obs)


# --------------------------------------------------------------------------- #
# Forbidden payload keys raise
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("key", sorted(FORBIDDEN_PAYLOAD_KEYS))
def test_forbidden_key_in_public_payload_raises(key: str) -> None:
    obs = _obs_with_payload({key: "leaked"})
    with pytest.raises(RankedPrivacyViolation, match=key):
        assert_ranked_observation_safe(obs)


@pytest.mark.parametrize("key", sorted(FORBIDDEN_PAYLOAD_KEYS))
def test_forbidden_key_in_private_payload_raises(key: str) -> None:
    obs = _obs_with_payload({key: "leaked"}, in_private=True)
    with pytest.raises(RankedPrivacyViolation, match=key):
        assert_ranked_observation_safe(obs)


def test_forbidden_key_nested_in_dict_raises() -> None:
    obs = _obs_with_payload({"meta": {"agent_build_id": "build-42"}})
    with pytest.raises(RankedPrivacyViolation, match="agent_build_id"):
        assert_ranked_observation_safe(obs)


def test_forbidden_key_nested_in_list_raises() -> None:
    obs = _obs_with_payload({"history": [{"model_id": "glm-4.7"}]})
    with pytest.raises(RankedPrivacyViolation, match="model_id"):
        assert_ranked_observation_safe(obs)


def test_forbidden_key_nested_in_tuple_raises() -> None:
    # Payloads coming out of typed Event models can include tuples after
    # round-tripping through model_dump(mode="python"); the walker must
    # descend into them too.
    obs = _obs_with_payload({"history": ({"provider": "cerebras"},)})
    with pytest.raises(RankedPrivacyViolation, match="provider"):
        assert_ranked_observation_safe(obs)


# --------------------------------------------------------------------------- #
# Cross-game leakage
# --------------------------------------------------------------------------- #


def test_foreign_game_id_in_payload_raises() -> None:
    obs = _obs_with_payload({"game_id": "G-some-other-game"})
    with pytest.raises(RankedPrivacyViolation, match="foreign game reference"):
        assert_ranked_observation_safe(obs)


def test_foreign_game_public_id_in_payload_raises() -> None:
    obs = _obs_with_payload({"game_public_id": "G-other"})
    with pytest.raises(RankedPrivacyViolation, match="foreign game reference"):
        assert_ranked_observation_safe(obs)


def test_matching_game_id_in_payload_is_allowed() -> None:
    # A payload echoing the obs's own game id is not a cross-game leak.
    obs = _obs_with_payload({"game_id": "G-privacy"})
    assert_ranked_observation_safe(obs)


# --------------------------------------------------------------------------- #
# Private memory token scan
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("token", sorted(FORBIDDEN_MEMORY_TOKENS))
def test_forbidden_token_in_private_memory_raises(token: str) -> None:
    state = _state()
    seat = state.seats[4]
    obs = build_observation(
        state,
        seat,
        EventLog(),
        mini7_v1,
        private_memory=f"my opponent's {token} is X",
    )
    with pytest.raises(RankedPrivacyViolation, match=token):
        assert_ranked_observation_safe(obs)


def test_forbidden_token_match_is_case_insensitive() -> None:
    state = _state()
    seat = state.seats[4]
    obs = build_observation(
        state, seat, EventLog(), mini7_v1, private_memory="The Model_ID was leaked"
    )
    with pytest.raises(RankedPrivacyViolation, match="model_id"):
        assert_ranked_observation_safe(obs)


def test_innocuous_private_memory_passes() -> None:
    state = _state()
    seat = state.seats[4]
    obs = build_observation(
        state, seat, EventLog(), mini7_v1, private_memory="I suspect P01 is mafia"
    )
    assert_ranked_observation_safe(obs)


# --------------------------------------------------------------------------- #
# Pure-core hygiene
# --------------------------------------------------------------------------- #


def test_observation_privacy_module_has_no_forbidden_imports() -> None:
    src = Path("src/padrino/core/observation_privacy.py").read_text()
    tree = ast.parse(src)
    forbidden = {
        "padrino.db",
        "padrino.llm",
        "padrino.api",
        "padrino.runner",
        "sqlalchemy",
        "litellm",
        "httpx",
        "random",
        "secrets",
        "time",
        "datetime",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in forbidden, alias.name
        elif isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[0]
            assert module not in forbidden, node.module
