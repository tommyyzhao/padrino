"""Golden-file + property tests for the public_event_v1 projection contract (US-086).

Two layers of protection:
1. Golden-file test — pins the exact public shape for a recorded game. Any
   intentional field change in the public_event_v1 schema requires a conscious
   update of tests/public/fixtures/projection_contract.json.
2. Hypothesis property test — for every internal event type with arbitrary
   payloads, the public projection must contain no key in PUBLIC_EVENT_FORBIDDEN_KEYS
   anywhere in the payload (regression-guard against the P0 role/faction leak).
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from padrino.core.engine import events as events_mod
from padrino.public.projection import (
    PUBLIC_EVENT_FORBIDDEN_KEYS,
    to_public_event_v1,
)

_FIXTURES_DIR = pathlib.Path(__file__).parent / "fixtures"
_GOLDEN_FILE = _FIXTURES_DIR / "projection_contract.json"

_PUBLIC_EVENT_V1_FIELDS = frozenset(
    {
        "schema_version",
        "sequence",
        "phase",
        "event_type",
        "visibility",
        "actor_player_id",
        "payload",
        "prev_event_hash",
        "event_hash",
    }
)

_VISIBILITY_BY_TYPE: dict[str, str] = {
    name: getattr(events_mod, name).model_fields["visibility"].default
    for name in events_mod.EVENT_TYPES
}


def _has_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            k in PUBLIC_EVENT_FORBIDDEN_KEYS or _has_forbidden_key(v) for k, v in value.items()
        )
    if isinstance(value, list | tuple):
        return any(_has_forbidden_key(item) for item in value)
    return False


# ---------------------------------------------------------------------------
# Golden-file test
# ---------------------------------------------------------------------------


def test_golden_file_exists() -> None:
    assert _GOLDEN_FILE.exists(), (
        f"Golden file missing: {_GOLDEN_FILE}. "
        "Run the projection manually and record the expected output."
    )


def test_projection_contract_golden() -> None:
    """Pin the exact public_event_v1 shape for a recorded game.

    If this test fails, it means the public schema has changed. Update
    tests/public/fixtures/projection_contract.json consciously.
    """
    data = json.loads(_GOLDEN_FILE.read_text(encoding="utf-8"))
    input_events: list[dict[str, Any]] = data["input_events"]
    expected: list[dict[str, Any]] = data["expected_projections"]

    actual = [ev for ev in (to_public_event_v1(e) for e in input_events) if ev is not None]

    assert len(actual) == len(expected), (
        f"Expected {len(expected)} projections, got {len(actual)}. "
        "Update the golden file if the projection logic changed intentionally."
    )
    for i, (got, want) in enumerate(zip(actual, expected, strict=True)):
        assert got == want, (
            f"Projection {i} mismatch.\n  Got:  {got}\n  Want: {want}\n"
            "Update tests/public/fixtures/projection_contract.json if the change is intentional."
        )


def test_golden_projections_have_exact_fields() -> None:
    """Every projected event has exactly the public_event_v1 field set — no more, no less."""
    data = json.loads(_GOLDEN_FILE.read_text(encoding="utf-8"))
    for ev in data["input_events"]:
        result = to_public_event_v1(ev)
        if result is None:
            continue
        assert set(result.keys()) == _PUBLIC_EVENT_V1_FIELDS, (
            f"Unexpected field set: {set(result.keys())} != {_PUBLIC_EVENT_V1_FIELDS}"
        )


def test_golden_schema_version_is_correct() -> None:
    data = json.loads(_GOLDEN_FILE.read_text(encoding="utf-8"))
    for ev in data["input_events"]:
        result = to_public_event_v1(ev)
        if result is not None:
            assert result["schema_version"] == "public_event_v1"


def test_golden_role_faction_stripped_from_player_eliminated() -> None:
    """Regression guard: PlayerEliminated mid-game must not leak role/faction."""
    event = {
        "sequence": 8,
        "event_type": "PlayerEliminated",
        "phase": "DAY_1",
        "visibility": "PUBLIC",
        "actor_player_id": None,
        "payload": {
            "public_player_id": "P01",
            "role": "MAFIOSO",
            "faction": "MAFIA",
            "cause": "DAY_VOTE",
        },
        "prev_event_hash": "1" * 64,
        "event_hash": "2" * 64,
    }
    result = to_public_event_v1(event)
    assert result is not None
    assert "role" not in result["payload"]
    assert "faction" not in result["payload"]
    assert result["payload"] == {"public_player_id": "P01", "cause": "DAY_VOTE"}


def test_system_and_private_events_return_none() -> None:
    """SYSTEM and PRIVATE events must be dropped (return None)."""
    system_event = {
        "sequence": 2,
        "event_type": "RolesAssigned",
        "phase": "SETUP",
        "visibility": "SYSTEM",
        "actor_player_id": None,
        "payload": {"assignments": [{"role": "MAFIOSO", "faction": "MAFIA"}]},
        "prev_event_hash": "",
        "event_hash": "a" * 64,
    }
    private_event = {
        "sequence": 5,
        "event_type": "PrivateMessageSubmitted",
        "phase": "DAY_1",
        "visibility": "PRIVATE",
        "actor_player_id": "P01",
        "payload": {"text": "secret mafia chat", "channel_id": "mafia"},
        "prev_event_hash": "b" * 64,
        "event_hash": "c" * 64,
    }
    assert to_public_event_v1(system_event) is None
    assert to_public_event_v1(private_event) is None


# ---------------------------------------------------------------------------
# Hypothesis property test
# ---------------------------------------------------------------------------

_forbidden_keys = st.sampled_from(sorted(PUBLIC_EVENT_FORBIDDEN_KEYS))
_plain_keys = st.sampled_from(["text", "target", "phase", "tally", "winner", "reason", "nested"])
_scalars = st.one_of(st.text(max_size=8), st.integers(), st.booleans(), st.none())


def _payloads() -> st.SearchStrategy[Any]:
    return st.recursive(
        _scalars,
        lambda children: st.one_of(
            st.dictionaries(st.one_of(_forbidden_keys, _plain_keys), children, max_size=4),
            st.lists(children, max_size=4),
        ),
        max_leaves=12,
    ).filter(lambda v: isinstance(v, dict))


@given(event_type=st.sampled_from(events_mod.EVENT_TYPES), payload=_payloads())
def test_no_forbidden_key_survives_in_public_projection(
    event_type: str, payload: dict[str, Any]
) -> None:
    """For every internal event type with arbitrary payloads, the public projection
    must contain no key in PUBLIC_EVENT_FORBIDDEN_KEYS in the payload."""
    visibility = _VISIBILITY_BY_TYPE[event_type]
    event = {
        "sequence": 1,
        "event_type": event_type,
        "phase": "DAY_1",
        "visibility": visibility,
        "actor_player_id": "P01",
        "payload": payload,
        "prev_event_hash": "a" * 64,
        "event_hash": "b" * 64,
    }
    result = to_public_event_v1(event)
    if visibility != "PUBLIC":
        assert result is None, f"{visibility} event {event_type} must be dropped"
    else:
        assert result is not None
        assert not _has_forbidden_key(result["payload"]), (
            f"Forbidden key survived public_event_v1 projection of {event_type}: {result['payload']}"
        )
        assert result["schema_version"] == "public_event_v1"
        assert set(result.keys()) == _PUBLIC_EVENT_V1_FIELDS
