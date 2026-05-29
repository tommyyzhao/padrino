"""Tests for the live-spectator projection (P0 #1 — mid-game role leak fix).

The projection is the single safe render path for a *non-terminal* game shown
to a non-player. The property tests assert, over every event type the engine
can emit and over arbitrary nested payloads, that:

* PRIVATE and SYSTEM events never survive, and
* surviving (PUBLIC) events carry no forbidden payload key — ``role`` /
  ``faction`` / model identity / ratings — anywhere in their nested payload.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from padrino.core.engine import events as events_mod
from padrino.core.observation_privacy import FORBIDDEN_PAYLOAD_KEYS
from padrino.core.spectator_projection import (
    SPECTATOR_VISIBLE_VISIBILITY,
    project_event_for_spectator,
    project_events_for_spectator,
    strip_forbidden,
)

# Canonical visibility per event type, derived from the frozen event models so
# the test can never drift from the engine's declared visibility.
_VISIBILITY_BY_TYPE: dict[str, str] = {
    name: getattr(events_mod, name).model_fields["visibility"].default
    for name in events_mod.EVENT_TYPES
}


def _has_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(k in FORBIDDEN_PAYLOAD_KEYS or _has_forbidden_key(v) for k, v in value.items())
    if isinstance(value, list | tuple):
        return any(_has_forbidden_key(item) for item in value)
    return False


# A payload generator that liberally sprinkles forbidden keys at every level.
_forbidden_keys = st.sampled_from(sorted(FORBIDDEN_PAYLOAD_KEYS))
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


def test_visibility_map_covers_every_event_type() -> None:
    assert set(_VISIBILITY_BY_TYPE) == set(events_mod.EVENT_TYPES)
    assert set(_VISIBILITY_BY_TYPE.values()) <= {"PUBLIC", "PRIVATE", "SYSTEM"}


@given(event_type=st.sampled_from(events_mod.EVENT_TYPES), payload=_payloads())
def test_no_forbidden_content_survives_for_any_event_type(
    event_type: str, payload: dict[str, Any]
) -> None:
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
    projected = project_event_for_spectator(event)
    if visibility != SPECTATOR_VISIBLE_VISIBILITY:
        assert projected is None, f"{visibility} event {event_type} must be dropped wholesale"
    else:
        assert projected is not None
        assert not _has_forbidden_key(projected["payload"]), (
            f"forbidden key survived projection of PUBLIC {event_type}"
        )
        # Non-payload fields are preserved verbatim.
        assert projected["event_hash"] == event["event_hash"]
        assert projected["sequence"] == event["sequence"]


def test_player_eliminated_role_and_faction_are_stripped() -> None:
    """The exact live bug: a PUBLIC PlayerEliminated leaking role/faction mid-game."""
    event = {
        "sequence": 12,
        "event_type": "PlayerEliminated",
        "phase": "DAY_2",
        "visibility": "PUBLIC",
        "actor_player_id": None,
        "payload": {
            "public_player_id": "P03",
            "role": "MAFIOSO",
            "faction": "MAFIA",
            "cause": "DAY_VOTE",
        },
        "prev_event_hash": "0" * 64,
        "event_hash": "1" * 64,
    }
    projected = project_event_for_spectator(event)
    assert projected is not None
    assert projected["payload"] == {"public_player_id": "P03", "cause": "DAY_VOTE"}


def test_roles_assigned_system_event_is_dropped_entirely() -> None:
    event = {
        "sequence": 2,
        "event_type": "RolesAssigned",
        "phase": "SETUP",
        "visibility": "SYSTEM",
        "actor_player_id": None,
        "payload": {
            "assignments": [
                {"public_player_id": "P01", "seat_index": 0, "role": "DOCTOR", "faction": "TOWN"},
            ]
        },
        "prev_event_hash": "",
        "event_hash": "x" * 64,
    }
    assert project_event_for_spectator(event) is None


def test_night_resolved_system_event_is_dropped_entirely() -> None:
    """NightResolved leaks the mafia kill target + doctor protect before the public reveal."""
    event = {
        "sequence": 8,
        "event_type": "NightResolved",
        "phase": "NIGHT_1",
        "visibility": "SYSTEM",
        "actor_player_id": None,
        "payload": {"eliminated": "P04", "protected": "P02", "mafia_kill_target": "P04"},
        "prev_event_hash": "y" * 64,
        "event_hash": "z" * 64,
    }
    assert project_event_for_spectator(event) is None


def test_unknown_visibility_fails_closed() -> None:
    event = {"event_type": "Mystery", "visibility": "WEIRD", "payload": {"x": 1}}
    assert project_event_for_spectator(event) is None
    event_missing = {"event_type": "Mystery", "payload": {"x": 1}}
    assert project_event_for_spectator(event_missing) is None


def test_non_dict_payload_normalizes_to_empty_dict() -> None:
    """A malformed PUBLIC event whose payload is not a dict yields an empty payload."""
    event = {"event_type": "Weird", "visibility": "PUBLIC", "payload": ["not", "a", "dict"]}
    projected = project_event_for_spectator(event)
    assert projected is not None
    assert projected["payload"] == {}


def test_project_events_preserves_order_and_drops_non_public() -> None:
    events = [
        {"sequence": 1, "visibility": "SYSTEM", "payload": {}},
        {"sequence": 2, "visibility": "PUBLIC", "payload": {"text": "hi"}},
        {"sequence": 3, "visibility": "PRIVATE", "payload": {"text": "secret"}},
        {
            "sequence": 4,
            "visibility": "PUBLIC",
            "payload": {"role": "MAFIOSO", "public_player_id": "P05"},
        },
    ]
    projected = project_events_for_spectator(events)
    assert [e["sequence"] for e in projected] == [2, 4]
    assert projected[1]["payload"] == {"public_player_id": "P05"}


def test_strip_forbidden_walks_lists_and_tuples() -> None:
    value = {
        "assignments": [
            {"public_player_id": "P01", "role": "DOCTOR"},
            {"public_player_id": "P02", "faction": "MAFIA"},
        ],
        "pair": ({"mu": 25.0, "keep": 1},),
    }
    assert strip_forbidden(value) == {
        "assignments": [{"public_player_id": "P01"}, {"public_player_id": "P02"}],
        "pair": ({"keep": 1},),
    }
