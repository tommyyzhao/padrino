"""Tests for immutable game-rules snapshots."""

from __future__ import annotations

import hashlib
import json
from typing import Any, cast

import pytest
from pydantic import ValidationError

from padrino.core.engine.phases import next_phase
from padrino.core.engine.role_assignment import assign_roles
from padrino.core.engine.state import GameState, Phase
from padrino.core.engine.win_conditions import check_win
from padrino.core.enums import PhaseKind
from padrino.core.rulesets import BUILTIN_RULESET_IDS, get_ruleset
from padrino.core.rulesets.snapshot import (
    BUILTIN_CADENCE_ID,
    GameRulesSnapshot,
    canonical_rules_dict,
    canonical_rules_hash,
    canonical_rules_json,
    custom_ruleset_id,
    snapshot_from_ruleset,
)


@pytest.mark.parametrize("ruleset_id", BUILTIN_RULESET_IDS)
def test_builtin_rulesets_project_to_behavior_equivalent_snapshots(ruleset_id: str) -> None:
    ruleset = get_ruleset(ruleset_id)
    snapshot = snapshot_from_ruleset(ruleset)

    assert snapshot.RULESET_ID == ruleset.RULESET_ID
    assert snapshot.RATING_CONTEXT_KIND is ruleset.RATING_CONTEXT_KIND
    assert snapshot.IS_CANONICAL is ruleset.IS_CANONICAL
    assert snapshot.RATING_CONTEXT_DISPLAY_LABEL == ruleset.RATING_CONTEXT_DISPLAY_LABEL
    assert snapshot.PLAYER_COUNT == ruleset.PLAYER_COUNT
    assert snapshot.ROLE_COUNTS == ruleset.ROLE_COUNTS
    assert snapshot.ROLE_FACTIONS == ruleset.ROLE_FACTIONS
    assert snapshot.WIN_CONDITIONS == ruleset.WIN_CONDITIONS
    assert snapshot.ALT_WIN_CONDITIONS == ruleset.ALT_WIN_CONDITIONS
    assert snapshot.SOLO_FACTIONS == ruleset.SOLO_FACTIONS
    assert snapshot.FACTION_MUTATION_ALLOWED is ruleset.FACTION_MUTATION_ALLOWED
    assert snapshot.KINGMAKING_OBJECTIVE is ruleset.KINGMAKING_OBJECTIVE
    assert snapshot.DISCUSSION_ROUNDS_PER_DAY == ruleset.DISCUSSION_ROUNDS_PER_DAY
    assert snapshot.MAX_DAYS == ruleset.MAX_DAYS
    assert snapshot.PUBLIC_MESSAGE_MAX_CHARS == ruleset.PUBLIC_MESSAGE_MAX_CHARS
    assert snapshot.PRIVATE_MESSAGE_MAX_CHARS == ruleset.PRIVATE_MESSAGE_MAX_CHARS
    assert snapshot.MEMORY_UPDATE_MAX_CHARS == ruleset.MEMORY_UPDATE_MAX_CHARS
    assert (
        snapshot.PUBLIC_TRANSCRIPT_RECENT_MESSAGE_LIMIT
        == ruleset.PUBLIC_TRANSCRIPT_RECENT_MESSAGE_LIMIT
    )
    assert snapshot.LLM_TIMEOUT_SECONDS == ruleset.LLM_TIMEOUT_SECONDS
    assert snapshot.TEMPERATURE == ruleset.TEMPERATURE
    assert snapshot.TOP_P == ruleset.TOP_P

    for role in ruleset.ROLE_COUNTS:
        assert snapshot.role_family_for(role) is ruleset.role_family_for(role)
        assert snapshot.faction_for(role) is ruleset.faction_for(role)

    seed = f"snapshot-parity:{ruleset_id}"
    protocol_snapshot = cast(Any, snapshot)
    assert assign_roles(seed, protocol_snapshot) == assign_roles(seed, ruleset)
    assert next_phase(Phase(kind=PhaseKind.SETUP, day=0, round=0), protocol_snapshot) == next_phase(
        Phase(kind=PhaseKind.SETUP, day=0, round=0),
        ruleset,
    )

    terminal = GameState(
        ruleset_id=ruleset.RULESET_ID,
        game_id="G-SNAPSHOT",
        game_seed=seed,
        day=ruleset.MAX_DAYS + 1,
        current_phase=Phase(kind=PhaseKind.TERMINAL, day=ruleset.MAX_DAYS + 1, round=0),
        seats=tuple(assign_roles(seed, ruleset)),
    )
    assert check_win(terminal, protocol_snapshot) == check_win(terminal, ruleset)


def test_snapshot_is_frozen() -> None:
    snapshot = snapshot_from_ruleset(get_ruleset("mini7_v1"))

    with pytest.raises(ValidationError, match="frozen"):
        snapshot.player_count = 8  # type: ignore[misc]

    with pytest.raises(ValidationError, match="frozen"):
        snapshot.role_counts[0].count = 9  # type: ignore[misc]


def test_canonical_rules_json_is_hash_addressed_compact_and_float_free() -> None:
    snapshot = snapshot_from_ruleset(get_ruleset("mini7_v1"))

    canonical_bytes = canonical_rules_json(snapshot)
    decoded = json.loads(canonical_bytes)

    assert canonical_bytes == canonical_rules_json(snapshot)
    assert b": " not in canonical_bytes
    assert b", " not in canonical_bytes
    assert canonical_rules_dict(snapshot)["role_counts"] == [
        {"count": 1, "role": "DETECTIVE"},
        {"count": 1, "role": "DOCTOR"},
        {"count": 2, "role": "MAFIA_GOON"},
        {"count": 3, "role": "VILLAGER"},
    ]
    _assert_no_float(decoded)

    expected_hash = hashlib.sha256(canonical_bytes).hexdigest()
    assert canonical_rules_hash(snapshot) == expected_hash
    assert custom_ruleset_id(snapshot) == f"custom:{expected_hash}"


def test_custom_snapshot_ruleset_id_is_derived_from_canonical_hash() -> None:
    snapshot = snapshot_from_ruleset(get_ruleset("mini7_v1")).model_copy(
        update={"ruleset_id": None}
    )

    assert custom_ruleset_id(snapshot) == snapshot.RULESET_ID


def test_mini7_snapshot_canonical_hash_is_byte_stable() -> None:
    snapshot = snapshot_from_ruleset(get_ruleset("mini7_v1"))

    assert (
        canonical_rules_hash(snapshot)
        == "f41cf3a33fcb576d5c3ec037190d76ff0f830365c55c36827cdd6a5b7ce79d45"
    )


def test_snapshot_validation_rejects_invalid_role_multiset() -> None:
    payload = snapshot_from_ruleset(get_ruleset("mini7_v1")).model_dump(mode="json")
    payload["player_count"] = 8

    with pytest.raises(ValidationError, match="role count total"):
        GameRulesSnapshot.model_validate(payload)


@pytest.mark.parametrize(
    ("path", "bad_value", "match"),
    [
        (("role_counts", 0, "role"), "GHOST", "role"),
        (("role_night_actions", 0, "action_type"), "BANISH", "action_type"),
        (("win_conditions", 0, "kind"), "COIN_FLIP", "kind"),
        (("cadence_id",), "unknown_cadence_v1", "cadence"),
    ],
)
def test_snapshot_validation_fails_closed_on_unknown_identifiers(
    path: tuple[str | int, ...],
    bad_value: str,
    match: str,
) -> None:
    payload = snapshot_from_ruleset(get_ruleset("mini7_v1")).model_dump(mode="json")
    _assign_path(payload, path, bad_value)

    with pytest.raises(ValidationError, match=match):
        GameRulesSnapshot.model_validate(payload)


def test_snapshot_validation_requires_complete_role_metadata() -> None:
    payload = snapshot_from_ruleset(get_ruleset("mini7_v1")).model_dump(mode="json")
    payload["role_factions"] = payload["role_factions"][:-1]

    with pytest.raises(ValidationError, match="role_factions"):
        GameRulesSnapshot.model_validate(payload)


def test_snapshot_cadence_id_is_pinned_for_builtins() -> None:
    snapshot = snapshot_from_ruleset(get_ruleset("bench10_v1"))

    assert snapshot.cadence_id == BUILTIN_CADENCE_ID
    assert canonical_rules_dict(snapshot)["cadence_id"] == BUILTIN_CADENCE_ID


def _assert_no_float(value: Any) -> None:
    if isinstance(value, float):
        raise AssertionError(f"canonical rules JSON contains float {value!r}")
    if isinstance(value, dict):
        for nested in value.values():
            _assert_no_float(nested)
    elif isinstance(value, list):
        for nested in value:
            _assert_no_float(nested)


def _assign_path(
    payload: dict[str, Any] | list[Any], path: tuple[str | int, ...], value: Any
) -> None:
    cursor: Any = payload
    for part in path[:-1]:
        cursor = cursor[part]
    cursor[path[-1]] = value
